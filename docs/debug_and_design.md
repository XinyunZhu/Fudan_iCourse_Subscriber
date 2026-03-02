# iCourse Subscriber 技术文档：Debug 历程与设计巧思

## 目录

- [一、项目背景](#一项目背景)
- [二、Debug 历程](#二debug-历程)
  - [2.1 VAD 整数溢出（sherpa-onnx circular buffer）](#21-vad-整数溢出sherpa-onnx-circular-buffer)
  - [2.2 LLM 429 Rate Limit 无 fallback](#22-llm-429-rate-limit-无-fallback)
  - [2.3 2.6GB 视频下载与 ffmpeg WebVPN 鉴权](#23-26gb-视频下载与-ffmpeg-webvpn-鉴权)
  - [2.4 LaTeX 公式渲染乱码](#24-latex-公式渲染乱码)
  - [2.5 崩溃后无法断点续传](#25-崩溃后无法断点续传)
  - [2.6 邮件发送失败无恢复](#26-邮件发送失败无恢复)
- [三、架构设计巧思](#三架构设计巧思)
  - [3.1 WebVPN AES-128-CFB URL 加密](#31-webvpn-aes-128-cfb-url-加密)
  - [3.2 CDN 视频 URL 签名算法（逆向工程）](#32-cdn-视频-url-签名算法逆向工程)
  - [3.3 七步 IDP 认证流程](#33-七步-idp-认证流程)
  - [3.4 加密数据库持久化（CI/CD）](#34-加密数据库持久化cicd)
  - [3.5 运行时数据库迁移](#35-运行时数据库迁移)
  - [3.6 流式音频管线](#36-流式音频管线)
  - [3.7 视频 URL 三级回退链](#37-视频-url-三级回退链)
  - [3.8 会话复活与登录重试](#38-会话复活与登录重试)

---

## 一、项目背景

iCourse Subscriber 是复旦大学智慧教学平台（iCourse）的自动化工具。它通过 WebVPN 登录校内网络，自动检测新课程录播，将视频转录为文字，用大语言模型生成课程总结，并通过邮件发送给用户。整个流程由 GitHub Actions 定时触发，无需本地运行。

核心流水线：

```
WebVPN 登录 → iCourse CAS 认证 → 检测新录播 → 流式音频转录 → LLM 总结 → 邮件发送
```

---

## 二、Debug 历程

### 2.1 VAD 整数溢出（sherpa-onnx circular buffer）

**现象**

CI 处理到第 9 个视频（累计约 80000 秒音频）时，sherpa-onnx 底层 C++ 输出大量错误：

```
circular-buffer.cc:Get:135 Invalid start_index: 2147455072. head_: 2147454560, tail_: -2147483648
```

注意 `2147455072` 逼近 `INT32_MAX (2147483647)`，而 `tail_` 已经溢出为负数。随后 onnxruntime 报出垃圾数值：

```
Sum of sizes in 'split' was 8847121603726204809
RuntimeError: NULL input supplied for input h
free()
```

进程级崩溃（可能 SIGSEGV），`try/except` 无法捕获。

**根因分析**

Silero VAD 的 `VoiceActivityDetector` 内部维护一个 circular buffer，其索引使用 `int32_t`。每次调用 `accept_waveform()` 累积采样计数。16kHz 采样率下：

```
8 个视频 × 10000 秒/个 × 16000 采样/秒 = 1,280,000,000 ≈ 1.28 × 10⁹
INT32_MAX = 2,147,483,647 ≈ 2.15 × 10⁹
```

处理到第 9 个视频时，累计采样数逼近 `INT32_MAX`，触发溢出。溢出后的负数索引导致 onnxruntime 读取垃圾内存，最终 `free()` 时崩溃。

**修复**

在 `Transcriber` 中新增 `_reset_vad()` 方法，每次转录前重新创建 VAD 对象，重置所有内部计数器：

```python
def _reset_vad(self):
    """Re-create VAD to reset internal counters (prevents INT32 overflow)."""
    self._vad = sherpa_onnx.VoiceActivityDetector(
        self._vad_config, buffer_size_in_seconds=120
    )
```

在 `_transcribe_from_cmd()` 入口处调用：

```python
def _transcribe_from_cmd(self, cmd, timeout=7200):
    self._init()
    self._reset_vad()  # 关键：每次转录前重置
    ...
```

注意：只重建 VAD，不重建 `OfflineRecognizer`（ASR 模型无状态累积问题，且加载耗时）。

---

### 2.2 LLM 429 Rate Limit 无 fallback

**现象**

处理 8 个视频的总结后，GLM-5 模型的 API 配额耗尽：

```
openai.RateLimitError: Error code: 429 - limit_requests
```

旧代码只配置了一个模型，直接抛异常，后续视频全部失败。

**修复**

`config.py` 中将单一模型改为有序列表：

```python
LLM_MODELS = [
    "ZhipuAI/GLM-5",        # 首选
    "MiniMax/MiniMax-M2.5",  # 备选 1
    "deepseek-ai/DeepSeek-V3.2",  # 备选 2
    "ZhipuAI/GLM-4.7",      # 备选 3
]
```

`Summarizer.summarize()` 遍历模型列表，捕获异常后自动尝试下一个：

```python
for model in self.models:
    try:
        response = self.client.chat.completions.create(model=model, ...)
        return (result, model)
    except Exception as e:
        errors.append(f"{model}: {e}")
raise RuntimeError("All LLM models failed:\n" + "\n".join(errors))
```

返回值从 `str` 改为 `tuple[str, str]`（summary, model_used），数据库中记录使用了哪个模型（`summary_model` 列），便于审计。

---

### 2.3 2.6GB 视频下载与 ffmpeg WebVPN 鉴权

**现象（第一阶段）**

旧代码下载完整视频文件（每个约 2.6GB），仅为提取音频。15 节课 = 39GB 下载量，CI 耗时 2 小时 50 分钟。

**优化思路**

用 ffmpeg 的 `-vn`（丢弃视频流）直接从 URL 流式提取音频。16kHz 单声道 PCM 仅约 60MB/小时，理论上可节省 97% 带宽。

**现象（第二阶段）**

改用 `transcribe_url(video_url)` 后，所有新视频转录全部失败。已有 transcript 的 stage-skip 正常，但新视频一个都无法处理。

**根因分析**

视频 CDN URL 需要通过 WebVPN 代理访问。旧代码中 `client.download_video()` 内部使用 `vpn.get(url)`，自动完成：
1. URL 转换：原始 URL → WebVPN 代理 URL（AES 加密主机名）
2. Cookie 注入：`requests.Session` 携带 `wengine_vpn_ticket` 等会话 cookie

而 `ffmpeg -i <URL>` 是独立进程，没有 WebVPN 的 session cookies，无法通过鉴权。

**修复**

ffmpeg 支持 `-headers` 参数注入 HTTP 头。方案：将 WebVPN URL 和 session cookies 提取出来传给 ffmpeg。

`ICourseClient` 新增 `get_stream_params()`：

```python
def get_stream_params(self, video_url: str) -> tuple[str, str]:
    vpn_url = get_vpn_url(video_url)     # AES 加密 URL 转换
    cookies = "; ".join(
        f"{c.name}={c.value}" for c in self.vpn.session.cookies
    )
    headers = f"Cookie: {cookies}\r\nUser-Agent: {config.USER_AGENT}\r\n"
    return vpn_url, headers
```

`Transcriber.transcribe_url()` 接受 `http_headers` 参数：

```python
def transcribe_url(self, url, timeout=7200, http_headers=None):
    cmd = ["ffmpeg"]
    if http_headers:
        cmd += ["-headers", http_headers]
    cmd += ["-reconnect", "1", ..., "-i", url, "-vn", ...]
```

调用链：

```python
vpn_url, http_headers = client.get_stream_params(video_url)
transcript = transcriber.transcribe_url(vpn_url, http_headers=http_headers)
```

ffmpeg 现在携带 WebVPN cookies 访问代理 URL，音频流式提取正常工作。

---

### 2.4 LaTeX 公式渲染乱码

**现象**

邮件中的数学公式渲染异常。手机端邮箱显示裂开的图片，电脑端邮箱显示乱码，例如：

```
递归式: T(n) = 2T (n/2) + (n)?0.36398024597265277.
分析:a=2,b=2,f(n)=n?0.6969471257599578。
```

`Θ` 符号变成 `?` 加随机浮点数。

**根因分析**

处理顺序错误。旧代码流程：

```
LLM 输出 Markdown → _md_to_html() → _latex_to_img_html()
```

问题在于 Python `markdown` 库将 `\` 视为转义字符。LLM 输出 `$\Theta(n)$` 经过 markdown 处理后变成 `$Theta(n)$`（反斜杠被吞掉）。损坏的 LaTeX 发送给 CodeCogs 渲染出乱码。

另外，SVG 格式在手机邮件客户端中普遍不被支持，导致图片直接裂开。

**修复**

重写 `_md_to_html()`，采用**提取-处理-还原**三步法：

```python
def _md_to_html(md_text: str) -> str:
    # Step 1: 提取 LaTeX，替换为占位符（\x00LATEX0\x00）
    latex_map = {}
    text = re.sub(r"\$\$(.+?)\$\$", _stash, md_text, flags=re.DOTALL)
    text = re.sub(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", _stash, text)

    # Step 2: Markdown 处理（占位符不含反斜杠，不会被破坏）
    html = markdown.markdown(text, extensions=_MD_EXTENSIONS)

    # Step 3: 用渲染后的 <img> 标签替换占位符
    for key, original in latex_map.items():
        html = html.replace(key, _render_latex_img(original))
    return html
```

同时将 SVG 改为 PNG 格式，并加 `\dpi{150}` 提升清晰度：

```
https://latex.codecogs.com/png.latex?\dpi{150}\inline%20\Theta(n)
```

PNG 格式在手机和桌面邮件客户端中均有良好兼容性。

---

### 2.5 崩溃后无法断点续传

**现象**

CI 在第 9 个视频崩溃后，下次运行仍从第 1 个视频重新开始下载和转录，即使前 8 个已有完整结果。

**根因**

旧代码的 `process_lecture()` 没有检查数据库中是否已有中间结果：

```python
# 旧代码：无条件下载、转录、总结
client.download_video(video_url, video_path)
transcript = transcriber.transcribe_video(video_path)
summary = summarizer.summarize(course_title, transcript)
```

**修复**

新增 stage-skipping 逻辑，检查 DB 中已有的中间结果：

```python
existing = db.get_lecture(sub_id)
has_transcript = existing and existing.get("transcript")
has_summary = existing and existing.get("summary")

if has_transcript:
    transcript = existing["transcript"]  # 跳过转录
else:
    transcript = transcriber.transcribe_url(...)

if has_summary:
    summary = existing["summary"]  # 跳过总结
else:
    summary, model = summarizer.summarize(...)
```

每个阶段失败时记录错误状态（`error_stage`, `error_msg`, `error_count`），成功后清除：

```python
except Exception as e:
    db.update_error(sub_id, "transcribe", str(e))
    raise
# 成功后
db.clear_error(sub_id)
```

---

### 2.6 邮件发送失败无恢复

**现象**

如果邮件发送失败（SMTP 超时等），已处理的总结不会在下次运行时重新发送。

**修复**

双重机制：

1. **发送重试**：`Emailer.send()` 内置 3 次重试 + 指数退避，返回 `bool` 表示成功/失败。

2. **未发送恢复**：`main.py` 在发送前查询 DB 中所有"已处理但未发送"的 lecture，合并到本次邮件中：

```python
unsent = db.get_unsent_lectures()  # processed_at IS NOT NULL AND emailed_at IS NULL
if unsent:
    seen = {item["sub_id"] for item in email_items}
    for row in unsent:
        if row["sub_id"] not in seen:  # 去重
            email_items.append({...})
```

只有 `send()` 返回 `True` 时才调用 `mark_emailed_batch()`，确保失败的邮件下次还会重试。

---

## 三、架构设计巧思

### 3.1 WebVPN AES-128-CFB URL 加密

WebVPN 使用 AES-128-CFB 加密目标主机名，将任意 URL 映射为 WebVPN 代理 URL：

```
原始:  https://icourse.fudan.edu.cn/courseapi/v3/...
代理:  https://webvpn.fudan.edu.cn/https/{IV_hex}{AES(hostname)}/courseapi/v3/...
```

- 密钥和 IV 均为 `wrdvpnisthebest!`（16 字节）
- 使用 CFB 模式（segment_size=128），支持任意长度主机名
- 非标准端口追加为 `-{port}` 后缀（如 `https-8080`）
- 实现了双向转换：`get_vpn_url()` 和 `get_ordinary_url()`

### 3.2 CDN 视频 URL 签名算法（逆向工程）

从 iCourse 前端 JS（`0.c6f283b4c2a6f87c4fa0.js`，模块 `"1P4N"`）逆向得到签名算法：

```
pathname  = urlparse(video_url).path
reversed_phone = phone[::-1]
hash_input = pathname + user_id + tenant_id + reversed_phone + timestamp
md5_hash  = md5(hash_input)
t_param   = f"{user_id}-{timestamp}-{md5_hash}"
signed_url = f"{video_url}?clientUUID={uuid4()}&t={t_param}"
```

关键细节：
- `phone` 需要**反转**（安全混淆）
- `timestamp` 来自 `get-sub-info` 响应的 `now` 字段（服务器时间）
- `clientUUID` 是随机 UUID v4

### 3.3 七步 IDP 认证流程

完整的校内统一身份认证流程：

| 步骤 | 操作 | 关键提取 |
|------|------|----------|
| 1 | GET `/idp/authCenter/authenticate` | 从重定向链提取 `lck` |
| 2 | POST `queryAuthMethods` | 提取 `authChainCode`（选 `userAndPwd` 模块）|
| 3 | GET `getJsPublicKey` | 获取 RSA 公钥（Base64）|
| 4 | RSA-PKCS1_v1_5 加密密码 | 本地操作 |
| 5 | POST `authExecute` | 获取 `loginToken`（注意：在顶层，不在 `data` 内）|
| 6 | POST `authnEngine` | 从 HTML 中正则提取 ticket URL |
| 7 | GET ticket URL | 建立会话，获得 `wengine_vpn_ticket` cookie |

iCourse CAS 认证在此基础上多一层：所有请求通过 WebVPN 代理，`entityId` 为 iCourse 地址而非 WebVPN 地址。

### 3.4 加密数据库持久化（CI/CD）

GitHub Actions 是无状态的，每次运行都是全新环境。数据库通过加密后存入 Git 仓库实现持久化：

```
运行前: data/icourse.db.enc → openssl dec → data/icourse.db
运行后: data/icourse.db → openssl enc → data/icourse.db.enc → git push
```

**巧妙设计**：

- **密钥派生**：`DB_KEY = STUID + UISPSW + DASHSCOPE_API_KEY + SMTP_PASSWORD`，多密钥拼接
- **Fork 安全**：不同 fork 的 secrets 不同 → 解密失败 → 静默创建空数据库，不影响新用户
- **变更检测**：用 MD5 对比运行前后的数据库，无变化则跳过 commit
- **`if: always()`**：即使 `python main.py` 崩溃，commit 步骤仍会执行，保存部分进度

### 3.5 运行时数据库迁移

不使用迁移文件，而是在 `_init_tables()` 中通过 `PRAGMA table_info` 检测现有列，按需 `ALTER TABLE ADD COLUMN`：

```python
existing = {row[1] for row in self.conn.execute("PRAGMA table_info(lectures)")}
for col, typedef in [("error_msg", "TEXT"), ("error_count", "INTEGER DEFAULT 0"), ...]:
    if col not in existing:
        self.conn.execute(f"ALTER TABLE lectures ADD COLUMN {col} {typedef}")
```

- 幂等：多次运行不会重复添加
- 向后兼容：旧数据库自动升级
- 零停机：SQLite 的 `ADD COLUMN` 不锁表

### 3.6 流式音频管线

```
CDN (WebVPN) ──HTTP──→ ffmpeg ──pipe──→ numpy ──32ms窗口──→ Silero VAD ──语音段──→ SenseVoice ASR
                        ↑                                                              ↓
                   -headers Cookie             文本累积 ←───────────────────────────── 识别结果
                   -vn (丢弃视频)
                   -ar 16000 -ac 1
                   -f f32le (原始PCM)
```

- **零磁盘 I/O**：音频通过 pipe 直接流入转录器，不写临时文件
- **分块读取**：每次读取 1 秒（16000 样本 × 4 字节 = 64KB），内存占用恒定
- **超时保护**：每秒检查一次是否超时，超时后 `proc.kill()` 防止僵尸进程
- **进程清理**：`finally` 块确保 ffmpeg 进程被正确终止

### 3.7 视频 URL 三级回退链

iCourse API 在不同字段返回视频 URL，代码按优先级逐级尝试：

```
优先级 1: video_list[*].preview_url  ← 最干净，无 /0/ 前缀
优先级 2: playurl dict               ← 可能有 /0/ 前缀，跳过 "now" 键
优先级 3: get-sub-detail → content.playback.url  ← 未签名，最后手段
```

对每个字段都做了类型检查（`isinstance(v, dict)` / `isinstance(v, str)`），防御 API 返回格式不一致。

### 3.8 会话复活与登录重试

WebVPN 会话在长时间处理中可能过期。代码在每个 lecture 处理前检查会话有效性：

```python
def _check_session(client):
    if client.check_alive():
        return client
    vpn = login_with_retry()  # 最多 5 次，每次全新 Session
    return ICourseClient(vpn)
```

`login_with_retry()` 每次创建全新的 `WebVPNSession()`（而非复用旧 session），因为 CAS 认证的重定向链依赖新的 cookie jar。

---

## 附录：改动文件清单

| 文件 | 改动概述 |
|------|----------|
| `src/database.py` | 运行时 schema 迁移，新增 error/model 追踪方法 |
| `src/config.py` | `LLM_MODEL` → `LLM_MODELS` 列表 |
| `src/summarizer.py` | 多模型 fallback 循环，返回 `(summary, model)` |
| `src/transcriber.py` | VAD 重置、`transcribe_url` + `http_headers`、超时保护 |
| `src/emailer.py` | LaTeX 提取-处理-还原三步法，PNG 格式，发送重试 |
| `src/icourse.py` | `get_stream_params()` 提取 WebVPN URL + cookies |
| `main.py` | stage-skipping、音频流式转录、错误记录、未发送恢复 |
| `tests/test_ffmpeg_stream.py` | ffmpeg WebVPN 鉴权流式测试 |
