import re
import smtplib
import time
import requests
from io import BytesIO
from PIL import Image
from collections import OrderedDict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from email.utils import formataddr
from urllib.parse import quote

import markdown

from . import config

_MD_EXTENSIONS = ["tables", "fenced_code", "nl2br", "sane_lists"]

_EMAIL_CSS = """\
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 15px;
    line-height: 1.7;
    color: #1a1a1a;
    max-width: 800px;
    margin: 0 auto;
    padding: 20px;
}
h2 {
    color: #2c3e50;
    border-bottom: 2px solid #3498db;
    padding-bottom: 8px;
    margin-top: 32px;
}
h3 {
    color: #34495e;
    margin-top: 24px;
}
h3 small {
    color: #7f8c8d;
    font-weight: normal;
}
h4 { color: #555; margin-top: 18px; }
hr {
    border: none;
    border-top: 1px solid #e0e0e0;
    margin: 28px 0;
}
strong { color: #c0392b; }
table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
}
th, td {
    border: 1px solid #ddd;
    padding: 8px 12px;
    text-align: left;
}
th {
    background: #f5f6fa;
    font-weight: 600;
}
tr:nth-child(even) { background: #fafafa; }
pre {
    background: #f4f4f4;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 12px 16px;
    overflow-x: auto;
    font-size: 13px;
    line-height: 1.5;
}
code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 13px;
}
p code {
    background: #f0f0f0;
    padding: 2px 5px;
    border-radius: 3px;
}
blockquote {
    border-left: 4px solid #3498db;
    margin: 12px 0;
    padding: 8px 16px;
    background: #f8f9fa;
    color: #555;
}
ul, ol { padding-left: 24px; }
li { margin-bottom: 4px; }
"""

_PDF_LATEX_SCALE = 2.5

_IMAGE_CACHE = {}

def _get_image_dimensions(url: str, dpi: int = 300) -> tuple:
    """获取并计算适配 300 DPI 的逻辑宽高"""
    if url in _IMAGE_CACHE:
        return _IMAGE_CACHE[url]
        
    try:
        # 300 DPI 是标准网页 96 DPI 的 3.125 倍
        scale_factor = dpi / 96.0
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        
        img = Image.open(BytesIO(response.content))
        # 计算逻辑宽高，最小为 1px
        logical_width = max(1, int(img.width / scale_factor))
        logical_height = max(1, int(img.height / scale_factor))
        
        _IMAGE_CACHE[url] = (logical_width, logical_height)
        return logical_width, logical_height
    except Exception as e:
        print(f"[LaTeX Render] 获取图片尺寸失败 {url}: {e}")
        return None, None
def _md_to_html(md_text: str, pdf_mode: bool = False) -> str:
    """Convert Markdown to styled HTML, rendering LaTeX math as images.

    Processing order: extract LaTeX → markdown convert → restore as <img>.
    This prevents the markdown engine from corrupting backslash escapes in LaTeX.

    Args:
        md_text: Markdown source text.
        pdf_mode: When True, scale LaTeX image dimensions to 1/2.5 of their
                  normal size to avoid oversized images in PDF output.
    """
    latex_map = {}
    counter = 0

    def _stash(match):
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = match.group(0)
        return key

    text = re.sub(r"\$\$(.+?)\$\$", _stash, md_text, flags=re.DOTALL)
    text = re.sub(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", _stash, text)

    html = markdown.markdown(text, extensions=_MD_EXTENSIONS)

    for key, original in latex_map.items():
        if original.startswith("$$"):
            # 块级公式处理
            latex_content = original[2:-2]
            # 注意：如果背景是纯白，可以加上 \bg{white} 参数以适配深色模式
            url = f"https://latex.codecogs.com/png.latex?\\dpi{{300}}\\bg{{white}}%20{quote(latex_content)}"
            w, h = _get_image_dimensions(url)
            
            if w and h:
                if pdf_mode:
                    w = max(1, int(w / _PDF_LATEX_SCALE))
                    h = max(1, int(h / _PDF_LATEX_SCALE))
                # 完美方案：写死 width 和 height
                img = (
                    f'<div style="text-align:center;margin:16px 0">'
                    f'<img src="{url}" alt="{escape(latex_content)}" '
                    f'width="{w}" height="{h}" style="vertical-align:middle; border:none; display:inline-block;"></div>'
                )
            else:
                # 降级方案：网络失败时依靠 CSS 限制
                img = (
                    f'<div style="text-align:center;margin:16px 0">'
                    f'<img src="{url}" alt="{escape(latex_content)}" '
                    f'style="vertical-align:middle; max-width:100%; height:auto;"></div>'
                )
        else:
            # 行内公式处理
            latex_content = original[1:-1]
            url = f"https://latex.codecogs.com/png.latex?\\dpi{{300}}\\bg{{white}}\\inline%20{quote(latex_content)}"
            w, h = _get_image_dimensions(url)
            
            if w and h:
                if pdf_mode:
                    w = max(1, int(w / _PDF_LATEX_SCALE))
                    h = max(1, int(h / _PDF_LATEX_SCALE))
                # 完美方案：写死 width 和 height，并使用 vertical-align 微调对齐基线
                img = (
                    f'<img src="{url}" alt="{escape(latex_content)}" '
                    f'width="{w}" height="{h}" style="vertical-align:-0.2em; border:none; margin:0 2px;">'
                )
            else:
                # 降级方案
                img = (
                    f'<img src="{url}" alt="{escape(latex_content)}" '
                    f'style="vertical-align:middle; height:1.3em; border:none; margin:0 2px;">'
                )
                
        html = html.replace(key, img)

    return html

class Emailer:
    """Send course summary emails via QQ SMTP SSL."""

    def __init__(self):
        self.host = config.SMTP_HOST
        self.port = config.SMTP_PORT
        self.sender = config.SMTP_EMAIL
        self.password = config.SMTP_PASSWORD
        self.receiver = config.RECEIVER_EMAIL

    def send(self, items: list[dict]) -> bool:
        """Send a single email containing all lecture summaries.

        Args:
            items: List of dicts, each with keys:
                   course_title, sub_title, date, summary

        Returns:
            True if email was sent successfully, False otherwise.
        """
        if not items:
            return True

        # Group by course (preserve insertion order)
        courses: OrderedDict[str, list[dict]] = OrderedDict()
        for item in items:
            courses.setdefault(item["course_title"], []).append(item)

        # Subject
        parts = [f"{ct} ({len(lecs)})" for ct, lecs in courses.items()]
        subject = f"[iCourse 课程内容更新] {', '.join(parts)}"

        # Plain text (Markdown as-is, readable without rendering)
        plain_sections = []
        for course_title, lectures in courses.items():
            plain_sections.append(f"{'=' * 40}")
            plain_sections.append(f"课程：{course_title}")
            plain_sections.append(f"{'=' * 40}")
            for lec in lectures:
                plain_sections.append(
                    f"\n--- {lec['sub_title']} ({lec['date']}) ---\n"
                )
                plain_sections.append(lec["summary"])
        plain = "\n".join(plain_sections)

        # HTML (Markdown → styled HTML with LaTeX rendering)
        body_parts = []
        for course_title, lectures in courses.items():
            body_parts.append(f"<h2>{escape(course_title)}</h2>")
            for lec in lectures:
                body_parts.append(
                    f"<h3>{escape(lec['sub_title'])} "
                    f"<small>({escape(lec['date'])})</small></h3>"
                )
                body_parts.append(_md_to_html(lec["summary"]))
                body_parts.append("<hr>")

        html = (
            "<!DOCTYPE html>"
            "<html><head><meta charset='utf-8'>"
            f"<style>{_EMAIL_CSS}</style>"
            "</head><body>"
            + "\n".join(body_parts)
            + "</body></html>"
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr(("iCourse Subscriber", self.sender))
        msg["To"] = self.receiver
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        # Retry with exponential backoff
        for attempt in range(3):
            try:
                with smtplib.SMTP_SSL(self.host, self.port) as server:
                    server.login(self.sender, self.password)
                    server.sendmail(self.sender, self.receiver, msg.as_string())
                print(f"[Emailer] Sent: {subject}")
                return True
            except Exception as e:
                print(f"[Emailer] Attempt {attempt + 1}/3 failed: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)

        print("[Emailer] All send attempts failed.")
        return False
