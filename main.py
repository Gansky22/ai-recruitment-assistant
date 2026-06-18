import json
import os
import re
import sqlite3
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageDraw, ImageFont
from starlette.requests import Request

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
EXPORT_DIR = BASE_DIR / "exports"
POSTER_DIR = BASE_DIR / "generated_posters"
DB_PATH = BASE_DIR / "history.db"
for d in [EXPORT_DIR, POSTER_DIR]:
    d.mkdir(exist_ok=True)

app = FastAPI(title="AI Recruitment Assistant V6")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/generated_posters", StaticFiles(directory=str(POSTER_DIR)), name="generated_posters")

TaskType = Literal["canva_v6", "poster_v5", "one_click_analysis", "title_review", "full_recruitment", "fb_final_copy", "facebook_targeting", "poster_text", "meta_ads", "translation", "client_reply"]
Provider = Literal["auto", "openai", "claude", "gemini"]
TASK_LABELS = {
    "canva_v6": "V6 Canva 模板文案输出",
    "poster_v5": "V5 一键生成4张Poster",
    "one_click_analysis": "一键完整分析",
    "title_review": "Job Title 审查与建议",
    "full_recruitment": "完整招聘广告 + Targeting",
    "fb_final_copy": "FB 最终中文招聘文案",
    "facebook_targeting": "Facebook Targeting 英文",
    "poster_text": "招聘海报文案",
    "meta_ads": "Meta广告建议",
    "translation": "中英马翻译",
    "client_reply": "客户回复优化",
}
POSTER_LAYOUTS = ["clean_center", "dark_focus", "navy_bold", "curve_light"]

SENSITIVE_REPLACES = [
    (r"(?i)\bpreferred chinese\b", "Must be able to communicate in Chinese"),
    (r"(?i)\bfemale preferred\b|\bmale preferred\b", "Welcome suitable candidates"),
    (r"高薪", "薪资"),
    (r"月入过万|轻松赚钱|稳赚|躺赚", "薪资与奖励依公司制度"),
    (r"保证收入|保证高薪", "薪资与奖励依公司制度"),
    (r"包录取|保证录取|马上录取|立即录取", "欢迎申请"),
    (r"你是女生吗[？?]?|你是男生吗[？?]?|你是华人吗[？?]?|你是年轻人吗[？?]?", "对这个职位有兴趣吗？"),
    (r"18\s*[-至到~]\s*\d+\s*岁?|\d+\s*岁以下|年龄\s*[:：]?\s*\d+\s*[-至到~]\s*\d+|Age\s*[:：]?\s*\d+\s*[-to]+\s*\d+", "欢迎有兴趣并符合职位要求者申请"),
    (r"只限女性|只限男性|只要女生|只要男生|女性优先|男性优先|女生优先|男生优先|Female only|Male only", "欢迎合适人选申请"),
    (r"只限华人|华人优先|只要华人|马来人优先|印度人优先|不要外劳|不要外国人|本地人而已|只限本地人|只限Malaysian|Chinese only", "需符合合法工作资格"),
]
PHONE_RE = r"(\+?6?01\d[- ]?\d{3,4}[- ]?\d{3,4}|0\d{1,2}[- ]?\d{3,4}[- ]?\d{3,4}|www\.wasap\.my/[^\s]+)"


def sanitize_line(line: str, remove_contact=False, remove_apply_now=False):
    text = (line or "").strip()
    actions = []
    if not text:
        return "", actions
    for pattern, replacement in SENSITIVE_REPLACES:
        if re.search(pattern, text, re.I):
            old = text
            text = re.sub(pattern, replacement, text, flags=re.I).strip()
            if old != text:
                actions.append(f"{old} -> {text or '[已移除]'}")
    if remove_contact and re.search(PHONE_RE, text, re.I):
        old = text
        text = re.sub(PHONE_RE, "", text, flags=re.I).strip(" -–—|,，")
        actions.append(f"移除联络方式：{old} -> {text or '[已移除]'}")
    if remove_apply_now and re.search(r"apply\s*now|立即申请|马上申请", text, re.I):
        old = text
        text = re.sub(r"apply\s*now|立即申请|马上申请", "", text, flags=re.I).strip(" -–—|,，")
        actions.append(f"移除 Apply Now：{old} -> {text or '[已移除]'}")
    text = re.sub(r"[ \t]+", " ", text).strip("|/ ")
    return text, actions


def sanitize_lines(lines, remove_contact=False, remove_apply_now=False):
    out, actions, seen = [], [], set()
    for line in lines:
        clean, acts = sanitize_line(str(line), remove_contact, remove_apply_now)
        actions.extend(acts)
        if clean and clean.lower() not in seen:
            out.append(clean)
            seen.add(clean.lower())
    return out, actions


def safety_report(actions):
    unique = []
    for a in actions:
        if a not in unique:
            unique.append(a)
    if not unique:
        return "【FB广告安全过滤报告】\n未发现需要替换的敏感字眼。"
    return "【FB广告安全过滤报告】\n已处理 " + str(len(unique)) + " 项：\n" + "\n".join("- " + a for a in unique[:20])


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS generations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        task_type TEXT,
        provider TEXT,
        selected_provider TEXT,
        job_title TEXT,
        company_name TEXT,
        location TEXT,
        salary TEXT,
        raw_job_info TEXT,
        job_info TEXT,
        result TEXT,
        poster_paths TEXT,
        uploaded_image_paths TEXT
    )
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(generations)").fetchall()]
    for col in ["poster_paths", "uploaded_image_paths"]:
        if col not in cols:
            conn.execute(f"ALTER TABLE generations ADD COLUMN {col} TEXT")
    conn.commit()
    conn.close()


init_db()


def first_nonempty(*values):
    for v in values:
        if v and str(v).strip():
            return str(v).strip()
    return ""


def extract_field(text, labels):
    for label in labels:
        m = re.search(rf"(?:^|\n)\s*{label}\s*[:：]\s*(.+)", text or "", re.I)
        if m:
            return m.group(1).strip()
    return ""


def extract_job_title(text):
    v = extract_field(text, ["职位名称", "Position Title", "Job Title"])
    if v:
        return v
    m = re.search(r"招聘\s*【?([^\n】]{2,60})】?", text or "")
    return m.group(1).strip() if m else ""


def extract_company_name(text):
    v = extract_field(text, ["公司名称", "Company Name", "公司"])
    if v:
        return v
    m = re.search(r"([A-Z][A-Za-z0-9&'() .,-]{2,80}(?:Sdn Bhd|Management Center|Studio|Centre|Center|Enterprise|Trading|Academy))", text or "")
    return m.group(1).strip() if m else ""


def extract_location(text):
    v = extract_field(text, ["工作地点", "Location", "地点", "地址", "Work Location"])
    if v:
        return v
    for city in ["Rawang", "Sungai Buloh", "Nusa Bestari", "Bukit Minyak", "Cheras", "Puchong", "Petaling Jaya", "Kota Damansara", "Johor Bahru", "Penang"]:
        if city.lower() in (text or "").lower():
            return city
    return ""


def extract_salary(text):
    v = extract_field(text, ["薪资", "Salary", "薪金"])
    if v:
        return v
    m = re.search(r"RM\s?[\d,]+(?:\s*[-至to]+\s*RM?\s?[\d,]+)?\+*", text or "", re.I)
    return m.group(0).strip() if m else ""


def extract_contact(text):
    v = extract_field(text, ["应聘联系电话", "联系电话", "Contact", "电话", "WhatsApp", "联系号码"])
    if v:
        return v
    m = re.search(r"(\+?6?01\d[- ]?\d{3,4}[- ]?\d{3,4})", text or "")
    return m.group(1).strip() if m else ""


def build_job_info(raw, job_title, company_name, industry, location, salary, working_hours, age, gender, race, benefits, responsibilities, requirements, company_info, contact_info, extra_notes):
    return f"""
【客户原始招聘资料】
{raw}

【补充资料】
职位名称：{job_title}
公司名称：{company_name}
行业：{industry}
工作地点：{location}
薪资：{salary}
工作时间：{working_hours}
年龄：{age}
性别：{gender}
种族：{race}
应聘联系资料：{contact_info}
福利待遇：{benefits}
工作职责：{responsibilities}
职位要求：{requirements}
公司简介：{company_info}
其他备注：{extra_notes}
"""


def choose_provider(task_type, provider):
    if provider != "auto":
        return provider
    if task_type == "translation":
        return "gemini"
    return "openai"


def call_openai(prompt):
    from openai import OpenAI
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return "OpenAI API Key 还没有设置。请到 Railway Variables 填写 OPENAI_API_KEY。"
    client = OpenAI(api_key=key)
    r = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[{"role": "system", "content": "你是马来西亚招聘广告、Meta广告、Canva海报文案专家。"}, {"role": "user", "content": prompt}],
        temperature=0.7,
    )
    return r.choices[0].message.content or ""


def call_claude(prompt):
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return "Claude API Key 还没有设置。"
    client = anthropic.Anthropic(api_key=key)
    r = client.messages.create(model=os.getenv("CLAUDE_MODEL", "claude-3-5-haiku-20241022"), max_tokens=3000, messages=[{"role": "user", "content": prompt}])
    return r.content[0].text


def call_gemini(prompt):
    import google.generativeai as genai
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return "Gemini API Key 还没有设置。"
    genai.configure(api_key=key)
    model = genai.GenerativeModel(os.getenv("GEMINI_MODEL", "gemini-1.5-flash"))
    r = model.generate_content(prompt)
    return r.text or ""


def call_ai(task_type, provider, prompt):
    selected = choose_provider(task_type, provider)
    try:
        if selected == "openai":
            return selected, call_openai(prompt)
        if selected == "claude":
            return selected, call_claude(prompt)
        if selected == "gemini":
            return selected, call_gemini(prompt)
    except Exception as e:
        return selected, f"发生错误：{e}"
    return selected, "未知 AI provider。"


def clean_json_text(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return text


def base_rules():
    return """
你是一位马来西亚资深招聘广告、Meta Ads、Canva 招聘海报文案专家。
重要规则：
- 文案自然、直接、有吸引力，不要机器翻译感。
- 不要夸大，不要保证录取，不要乱编不存在资料。
- 不要写年龄、性别、种族、国籍限制。
- 不要直接点名个人属性，例如 你是女生吗、你是华人吗。
- 不要写高薪、保证收入、包录取、稳赚、轻松赚钱。
- 如果客户资料有敏感条件，请转成更安全、中性的表达。
"""


def build_general_prompt(task_type, job_info):
    if task_type == "title_review":
        task = "判断 Job Title 是否准确，并给建议标题。"
    elif task_type == "facebook_targeting":
        task = "只输出 Facebook / Meta Ads targeting 建议，全部使用英文，包含 Job Titles、Study Fields、Interests、Behaviors、Location。"
    elif task_type == "translation":
        task = "翻译成 English 和 Bahasa Melayu，适合马来西亚招聘。"
    elif task_type == "client_reply":
        task = "优化客户回复，给中文、英文、马来文版本。"
    elif task_type == "meta_ads":
        task = "输出 Meta 招聘广告投放建议、文案、Messenger 回复流程。"
    elif task_type == "poster_text":
        task = "输出 6 个不同方向的 1:1 招聘 Poster 短文案。"
    else:
        task = "输出完整招聘广告、Job Title 审查、FB文案、英文文案、马来文文案、Targeting、Poster文案、筛选问题。"
    return f"{base_rules()}\n任务：{task}\n资料：\n{job_info}"


def fallback_posters(job_title, location, salary):
    title = job_title or "招聘职位"
    hooks = ["你正在寻找新机会吗？", "想找稳定发展的工作吗？", "这里有适合你的发展舞台！", "加入我们，开启新阶段！"]
    templates = ["模板A_纯文字大字", "模板B_深色背景", "模板C_品牌色块", "模板D_简洁留白"]
    posters = []
    for i in range(4):
        lines = []
        if salary:
            lines.append(f"薪资：{salary}")
        if location:
            lines.append(f"地点：{location}")
        lines += ["提供培训与成长机会", "欢迎有兴趣者加入", "欢迎来询问"]
        posters.append({"template_name": templates[i], "hook": hooks[i], "small_label": "我们正在招聘", "job_title": title, "lines": lines[:5], "layout": POSTER_LAYOUTS[i]})
    return posters


def generate_poster_plan(job_info, provider, job_title, location, salary):
    prompt = f"""
{base_rules()}
请根据资料生成 4 张 1:1 招聘 Poster 文案，严格输出 JSON：{{"posters":[...]}}
每个 poster：hook、small_label、job_title、lines、layout。
layout 只能用：clean_center, dark_focus, navy_bold, curve_light。
每个 hook 不同；不写联络号码；不写 Apply Now；总文字约 8 行内。
资料：
{job_info}
"""
    selected, raw = call_ai("poster_v5", provider, prompt)
    actions = []
    try:
        data = json.loads(clean_json_text(raw))
        posters = data.get("posters", [])
        if len(posters) != 4:
            raise ValueError("need 4 posters")
    except Exception:
        posters = fallback_posters(job_title, location, salary)
    used = set()
    safe = []
    for i, p in enumerate(posters[:4]):
        hook, a1 = sanitize_line(p.get("hook", ""), True, True)
        label, a2 = sanitize_line(p.get("small_label", ""), True, True)
        title, a3 = sanitize_line(p.get("job_title", job_title or "招聘职位"), True, True)
        lines, a4 = sanitize_lines(p.get("lines", [])[:5], True, True)
        actions += a1 + a2 + a3 + a4
        layout = p.get("layout") if p.get("layout") in POSTER_LAYOUTS and p.get("layout") not in used else POSTER_LAYOUTS[i]
        used.add(layout)
        safe.append({"hook": hook or fallback_posters(job_title, location, salary)[i]["hook"], "small_label": label or "我们正在招聘", "job_title": title or job_title or "招聘职位", "lines": lines or ["欢迎有兴趣者加入", "欢迎来询问"], "layout": layout})
    return selected, {"posters": safe}, actions


def generate_canva_v6_output(job_info, provider, job_title, location, salary):
    prompt = f"""
{base_rules()}
请根据资料输出 4 套可直接复制进 Canva 招聘模板的文案，严格输出 JSON：{{"posters":[...]}}
每个 poster 必须有：template_name、hook、small_label、job_title、lines、copy_block。
template_name 用：模板A_纯文字大字、模板B_深色背景、模板C_品牌色块、模板D_简洁留白。
每套 6-8 行；不要写联络号码；不要写 Apply Now。
资料：
{job_info}
"""
    selected, raw = call_ai("canva_v6", provider, prompt)
    actions = []
    try:
        data = json.loads(clean_json_text(raw))
        posters = data.get("posters", [])
        if len(posters) != 4:
            raise ValueError("need 4")
    except Exception:
        posters = fallback_posters(job_title, location, salary)
        for i, p in enumerate(posters):
            p["copy_block"] = "\n".join([p["hook"], p["small_label"], p["job_title"]] + p["lines"])
    template_names = ["模板A_纯文字大字", "模板B_深色背景", "模板C_品牌色块", "模板D_简洁留白"]
    output = ["【V6 Canva 模板文案输出】", "用途：把每个字段复制进 Canva 模板对应文字框。", ""]
    for i, p in enumerate(posters[:4]):
        hook, a1 = sanitize_line(p.get("hook", ""), True, True)
        label, a2 = sanitize_line(p.get("small_label", ""), True, True)
        title, a3 = sanitize_line(p.get("job_title", job_title or "招聘职位"), True, True)
        lines, a4 = sanitize_lines(p.get("lines", [])[:5], True, True)
        actions += a1 + a2 + a3 + a4
        hook = hook or fallback_posters(job_title, location, salary)[i]["hook"]
        label = label or "我们正在招聘"
        title = title or job_title or "招聘职位"
        if not lines:
            lines = ["欢迎有兴趣者加入", "欢迎来询问"]
        block = "\n".join([hook, label, title] + lines)
        output += [f"Poster {i+1}｜{p.get('template_name') or template_names[i]}", f"Hook：{hook}", f"招聘字眼：{label}", f"Job Title：{title}"]
        output += [f"Line {j+1}：{line}" for j, line in enumerate(lines)]
        output += ["", "可直接复制到 Canva：", block, ""]
    output.append(safety_report(actions))
    return selected, "\n".join(output), actions


def get_font(size, bold=False):
    candidates = []
    if os.name == "nt":
        candidates += ["C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"]
    candidates += ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for f in candidates:
        if Path(f).exists():
            try:
                return ImageFont.truetype(f, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def wrap(draw, text, font, max_width):
    out, cur = [], ""
    for ch in str(text):
        trial = cur + ch
        if draw.textbbox((0,0), trial, font=font)[2] <= max_width or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = ch
    if cur:
        out.append(cur)
    return out


def draw_center(draw, text, font, y, fill, max_width=940):
    for line in wrap(draw, text, font, max_width):
        box = draw.textbbox((0,0), line, font=font)
        draw.text((540 - (box[2]-box[0])/2, y), line, font=font, fill=fill)
        y += (box[3]-box[1]) + 10
    return y


def create_single_poster(poster, output_path, idx):
    W = H = 1080
    layout = poster.get("layout", POSTER_LAYOUTS[idx])
    bg = (248,248,248) if layout != "dark_focus" else (18,26,43)
    if layout == "navy_bold":
        bg = (28,41,66)
    img = Image.new("RGB", (W,H), bg)
    draw = ImageDraw.Draw(img)
    if layout != "dark_focus" and layout != "navy_bold":
        draw.ellipse((-120,-90,520,220), fill=(255,220,225))
        draw.ellipse((720,820,1220,1220), fill=(255,235,185))
    if layout == "navy_bold":
        draw.pieslice((800,-160,1240,240), 205, 360, fill=(205,20,38))
        draw.pieslice((760,900,1240,1280), 20, 158, fill=(205,20,38))
    fill = (255,255,255) if layout in ["dark_focus", "navy_bold"] else (20,20,20)
    title_font = get_font(74, True)
    hook_font = get_font(48, True)
    body_font = get_font(44, True)
    items = [poster.get("hook", ""), poster.get("small_label", "我们正在招聘"), poster.get("job_title", "招聘职位")] + poster.get("lines", [])[:5]
    y = 130
    for i, item in enumerate(items):
        font = title_font if i == 2 else (hook_font if i < 2 else body_font)
        y = draw_center(draw, item, font, y, fill)
        y += 12
    img.save(output_path, "PNG")


def create_posters(record_id, plan):
    paths = []
    for i, p in enumerate(plan.get("posters", [])[:4]):
        out = POSTER_DIR / f"record_{record_id}_poster_{i+1}.png"
        create_single_poster(p, out, i)
        paths.append(str(out.relative_to(BASE_DIR)).replace("\\", "/"))
    return paths


def save_history(data):
    conn = db_connect()
    cur = conn.execute("""
    INSERT INTO generations (created_at, task_type, provider, selected_provider, job_title, company_name, location, salary, raw_job_info, job_info, result, poster_paths, uploaded_image_paths)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), data.get("task_type",""), data.get("provider",""), data.get("selected_provider",""), data.get("job_title",""), data.get("company_name",""), data.get("location",""), data.get("salary",""), data.get("raw_job_info",""), data.get("job_info",""), data.get("result",""), data.get("poster_paths",""), data.get("uploaded_image_paths","")))
    conn.commit()
    rid = int(cur.lastrowid)
    conn.close()
    return rid


def update_record_posters(record_id, poster_paths):
    conn = db_connect()
    conn.execute("UPDATE generations SET poster_paths = ? WHERE id = ?", (json.dumps(poster_paths, ensure_ascii=False), record_id))
    conn.commit()
    conn.close()


def get_record(record_id):
    conn = db_connect()
    row = conn.execute("SELECT * FROM generations WHERE id = ?", (record_id,)).fetchone()
    conn.close()
    return row


def safe_filename(text):
    return re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", text or "recruitment")[:80]


def create_docx(record):
    from docx import Document
    path = EXPORT_DIR / (safe_filename(f"{record['job_title'] or 'recruitment'}_{record['id']}") + ".docx")
    doc = Document()
    doc.add_heading("AI Recruitment Assistant V6", level=1)
    for item in ["created_at", "task_type", "selected_provider", "job_title", "company_name", "location", "salary"]:
        doc.add_paragraph(f"{item}: {record[item] or '-'}")
    doc.add_heading("Generated Result", level=2)
    for line in (record["result"] or "").split("\n"):
        doc.add_paragraph(line)
    try:
        posters = json.loads(record["poster_paths"] or "[]")
        for p in posters:
            ap = BASE_DIR / p
            if ap.exists():
                doc.add_picture(str(ap), width=None)
    except Exception:
        pass
    doc.save(path)
    return path


def create_pdf(record):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    path = EXPORT_DIR / (safe_filename(f"{record['job_title'] or 'recruitment'}_{record['id']}") + ".pdf")
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        font = "STSong-Light"
    except Exception:
        font = "Helvetica"
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="CJK", parent=styles["BodyText"], fontName=font, fontSize=10, leading=15))
    story = [Paragraph("AI Recruitment Assistant V6", styles["Title"]), Spacer(1, 12)]
    for line in (record["result"] or "").split("\n"):
        story.append(Paragraph(escape(line) if line else " ", styles["CJK"]))
    SimpleDocTemplate(str(path), pagesize=A4).build(story)
    return path


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {"result": None, "selected_provider": None, "record_id": None, "poster_files": [], "task_labels": TASK_LABELS})


@app.post("/generate", response_class=HTMLResponse)
async def generate(request: Request, task_type: str = Form(...), provider: str = Form("auto"), raw_job_info: str = Form(""), job_title: str = Form(""), company_name: str = Form(""), industry: str = Form(""), location: str = Form(""), salary: str = Form(""), working_hours: str = Form(""), age: str = Form(""), gender: str = Form(""), race: str = Form(""), benefits: str = Form(""), responsibilities: str = Form(""), requirements: str = Form(""), company_info: str = Form(""), contact_info: str = Form(""), extra_notes: str = Form("")):
    job_title = first_nonempty(job_title, extract_job_title(raw_job_info))
    company_name = first_nonempty(company_name, extract_company_name(raw_job_info))
    location = first_nonempty(location, extract_location(raw_job_info))
    salary = first_nonempty(salary, extract_salary(raw_job_info))
    contact_info = first_nonempty(contact_info, extract_contact(raw_job_info))
    job_info = build_job_info(raw_job_info, job_title, company_name, industry, location, salary, working_hours, age, gender, race, benefits, responsibilities, requirements, company_info, contact_info, extra_notes)
    poster_files = []
    if task_type == "canva_v6":
        selected, result, actions = generate_canva_v6_output(job_info, provider, job_title, location, salary)
    elif task_type == "poster_v5":
        selected, plan, actions = generate_poster_plan(job_info, provider, job_title, location, salary)
        lines = ["【V5 Poster 生成结果】"]
        for i, p in enumerate(plan.get("posters", []), 1):
            lines += [f"Poster {i}", f"Hook：{p.get('hook','')}", f"职位：{p.get('job_title','')}", "海报文字：" + " | ".join(p.get("lines", [])), ""]
        lines.append(safety_report(actions))
        result = "\n".join(lines)
    else:
        prompt = build_general_prompt(task_type, job_info)
        selected, raw = call_ai(task_type, provider, prompt)
        clean, actions = sanitize_lines((raw or "").splitlines(), False, False)
        result = "\n".join(clean) + "\n\n" + safety_report(actions)
    record_id = save_history({"task_type": task_type, "provider": provider, "selected_provider": selected, "job_title": job_title, "company_name": company_name or "Unknown Company", "location": location, "salary": salary, "raw_job_info": raw_job_info, "job_info": job_info, "result": result})
    if task_type == "poster_v5":
        poster_files = create_posters(record_id, plan)
        update_record_posters(record_id, poster_files)
    return templates.TemplateResponse(request, "index.html", {"result": result, "selected_provider": selected, "record_id": record_id, "poster_files": poster_files, "task_labels": TASK_LABELS, "task_type": task_type, "provider": provider, "raw_job_info": raw_job_info, "job_title": job_title, "company_name": company_name, "industry": industry, "location": location, "salary": salary, "working_hours": working_hours, "age": age, "gender": gender, "race": race, "benefits": benefits, "responsibilities": responsibilities, "requirements": requirements, "company_info": company_info, "contact_info": contact_info, "extra_notes": extra_notes})


@app.get("/history", response_class=HTMLResponse)
def history(request: Request, q: str = ""):
    conn = db_connect()
    if q:
        like = f"%{q}%"
        rows = conn.execute("SELECT * FROM generations WHERE job_title LIKE ? OR company_name LIKE ? OR location LIKE ? OR raw_job_info LIKE ? OR result LIKE ? ORDER BY id DESC LIMIT 100", (like, like, like, like, like)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM generations ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return templates.TemplateResponse(request, "history.html", {"rows": rows, "q": q, "task_labels": TASK_LABELS})


@app.get("/record/{record_id}", response_class=HTMLResponse)
def view_record(request: Request, record_id: int):
    record = get_record(record_id)
    if not record:
        return HTMLResponse("Record not found", status_code=404)
    try:
        poster_files = json.loads(record["poster_paths"] or "[]")
    except Exception:
        poster_files = []
    return templates.TemplateResponse(request, "record.html", {"record": record, "task_labels": TASK_LABELS, "poster_files": poster_files})


@app.get("/export/docx/{record_id}")
def export_docx(record_id: int):
    record = get_record(record_id)
    if not record:
        return HTMLResponse("Record not found", status_code=404)
    path = create_docx(record)
    return FileResponse(str(path), filename=path.name, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.get("/export/pdf/{record_id}")
def export_pdf(record_id: int):
    record = get_record(record_id)
    if not record:
        return HTMLResponse("Record not found", status_code=404)
    path = create_pdf(record)
    return FileResponse(str(path), filename=path.name, media_type="application/pdf")


@app.post("/delete/{record_id}")
def delete_record(record_id: int):
    conn = db_connect()
    conn.execute("DELETE FROM generations WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/history", status_code=303)
