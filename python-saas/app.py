from flask import (
    Flask, request, render_template, redirect,
    session, jsonify, Response, stream_with_context
)
import psycopg2
import os
import io
import re
import json
import markdown
import bcrypt
import pdfplumber
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI

# PDF generation
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer,
    Table, TableStyle, HRFlowable
)
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY

# Image OCR (optional)
try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# ---------- ENV ----------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "aura_secret_2025")
DATABASE_URL = os.getenv("DATABASE_URL")

# ---------- AI CLIENT (Groq) ----------
client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)
MODEL       = "llama-3.1-8b-instant"
MODEL_LARGE = "llama-3.3-70b-versatile"


# ════════════════════════════════════════════
#  AI HELPERS
# ════════════════════════════════════════════

def query_ai_stream(prompt, system="You are AuraAI, a helpful assistant. Answer clearly and thoroughly.", history=None):
    try:
        messages = [{"role": "system", "content": system}]
        if history:
            for h in history[-10:]:  # last 10 turns for context
                messages.append({"role": "user", "content": h["user"]})
                messages.append({"role": "assistant", "content": h["bot_plain"] or ""})
        messages.append({"role": "user", "content": prompt})

        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=1024,
            stream=True
        )
        for chunk in resp:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    except Exception as e:
        yield f"\n\n*Error: {e}*"


def query_ai(prompt, system="Answer clearly and briefly.", large=False):
    try:
        model = MODEL_LARGE if large else MODEL
        res = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt}
            ],
            max_tokens=3000 if large else 800
        )
        return res.choices[0].message.content.strip()
    except Exception as e:
        return f"Error: {e}"


def generate_chat_title(message):
    try:
        res = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "Generate a short chat title in max 6 words. No quotes, no punctuation at end."},
                {"role": "user",   "content": message[:300]}
            ],
            max_tokens=20
        )
        return res.choices[0].message.content.strip()
    except:
        return message[:40]


def generate_image_url(prompt):
    clean = re.sub(r"[^\w\s-]", "", prompt)[:200].replace(" ", "%20")
    return f"https://image.pollinations.ai/prompt/{clean}?width=800&height=500&nologo=true"


# ════════════════════════════════════════════
#  NOTE / Q&A GENERATION
# ════════════════════════════════════════════

QA_SYSTEM = """You are an expert exam question generator and study notes creator.
Given source material, you MUST output a JSON object ONLY (no markdown, no extra text) with this exact structure:
{
  "title": "Topic title here",
  "summary": "2-3 sentence overview of the topic",
  "key_points": ["point 1", "point 2", "point 3", ...],
  "questions": [
    {
      "q": "Question text here?",
      "a": "Detailed answer here.",
      "type": "short"
    },
    ...
  ]
}
Generate 8-15 questions. Mix types: short answer, long answer, true/false, fill-in-the-blank.
For fill-in-the-blank format the question as: "The process of ______ is called photosynthesis."
For true/false start with "True or False:"
Answers must be thorough and educational. Output ONLY valid JSON."""


def generate_notes_from_text(source_text: str, extra_instruction: str = "") -> dict:
    prompt = f"""Source material:
---
{source_text[:6000]}
---
{extra_instruction}
Generate comprehensive study notes and questions from the above material."""

    raw = query_ai(prompt, system=QA_SYSTEM, large=True)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"```$", "", raw).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "title": "Study Notes",
            "summary": "AI-generated notes from your document.",
            "key_points": ["Review the source material carefully."],
            "questions": [
                {"q": "Summarise the main topic of this document.", "a": raw[:500], "type": "long"}
            ]
        }


# ════════════════════════════════════════════
#  QUIZ / FLASHCARD GENERATION
# ════════════════════════════════════════════

QUIZ_SYSTEM = """You are a quiz generator. Output ONLY a JSON array of MCQ objects, no markdown, no extra text:
[
  {
    "q": "Question text?",
    "options": ["A) option1", "B) option2", "C) option3", "D) option4"],
    "answer": "A",
    "explanation": "Brief explanation of why A is correct."
  },
  ...
]
Generate exactly 10 multiple-choice questions. Output ONLY valid JSON array."""

FLASHCARD_SYSTEM = """You are a flashcard generator. Output ONLY a JSON array, no markdown:
[
  {"front": "Term or concept", "back": "Definition or explanation"},
  ...
]
Generate 15-20 flashcards. Output ONLY valid JSON array."""

SUMMARIZE_SYSTEM = """You are a text summarizer. Output ONLY a JSON object, no markdown:
{
  "tldr": "One sentence summary",
  "bullets": ["key point 1", "key point 2", "key point 3", ...],
  "full_summary": "2-3 paragraph detailed summary"
}
Output ONLY valid JSON."""

EXPLAIN_SYSTEM = """You are AuraAI, an expert teacher. Explain the concept clearly.
Format your response using markdown with headers, bullet points and examples.
Be thorough but accessible. Use analogies and real-world examples."""

TRANSLATE_SYSTEM = """You are a professional translator. 
Translate the given text to the requested language accurately and naturally.
Respond with ONLY the translation, nothing else."""

GRAMMAR_SYSTEM = """You are a professional writing editor. 
Check the text for grammar, spelling, punctuation and style issues.
Return ONLY a JSON object:
{
  "corrected": "The fully corrected text here",
  "issues": ["issue 1 description", "issue 2 description", ...],
  "score": 85
}
Output ONLY valid JSON."""

CODE_SYSTEM = """You are AuraAI, an expert programmer. 
Help with code: debug, explain, generate, review, or convert as requested.
Use markdown code blocks with proper language tags.
Always explain your code clearly."""


def generate_quiz(source_text: str) -> list:
    prompt = f"Generate a 10-question multiple choice quiz from this content:\n\n{source_text[:4000]}"
    raw = query_ai(prompt, system=QUIZ_SYSTEM, large=True)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except:
        return []


def generate_flashcards(source_text: str) -> list:
    prompt = f"Generate flashcards from this content:\n\n{source_text[:4000]}"
    raw = query_ai(prompt, system=FLASHCARD_SYSTEM, large=True)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except:
        return []


# ════════════════════════════════════════════
#  PDF BUILDER  (ReportLab)
# ════════════════════════════════════════════

def build_notes_pdf(data: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
        title=data.get("title", "Study Notes"),
        author="AuraAI"
    )

    TEAL  = colors.HexColor("#00d4aa")
    NAVY  = colors.HexColor("#0c0f1a")
    SLATE = colors.HexColor("#1e2a3a")
    LIGHT = colors.HexColor("#dde4f0")
    MUTED = colors.HexColor("#8896ae")
    DARK  = colors.HexColor("#07090f")

    title_style = ParagraphStyle("ATitle", fontName="Helvetica-Bold", fontSize=22,
        textColor=TEAL, spaceAfter=6, alignment=TA_CENTER, leading=28)
    sub_style = ParagraphStyle("ASub", fontName="Helvetica", fontSize=10,
        textColor=MUTED, spaceAfter=4, alignment=TA_CENTER)
    section_style = ParagraphStyle("ASection", fontName="Helvetica-Bold", fontSize=13,
        textColor=TEAL, spaceBefore=14, spaceAfter=6, leading=16)
    body_style = ParagraphStyle("ABody", fontName="Helvetica", fontSize=10,
        textColor=LIGHT, leading=15, spaceAfter=5, alignment=TA_JUSTIFY)
    bullet_style = ParagraphStyle("ABullet", fontName="Helvetica", fontSize=10,
        textColor=LIGHT, leading=14, spaceAfter=3, leftIndent=14)
    q_style = ParagraphStyle("AQ", fontName="Helvetica-Bold", fontSize=10,
        textColor=LIGHT, leading=14, spaceBefore=8, spaceAfter=3)
    a_style = ParagraphStyle("AA", fontName="Helvetica", fontSize=10,
        textColor=colors.HexColor("#a8bbd4"), leading=14, leftIndent=12, spaceAfter=4)
    badge_style = ParagraphStyle("ABadge", fontName="Helvetica-Bold", fontSize=7,
        textColor=DARK, alignment=TA_CENTER)

    story = []

    header_data = [[Paragraph(data.get("title", "Study Notes"), title_style)]]
    header_table = Table(header_data, colWidths=[17*cm])
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), NAVY),
        ("ROWPADDING", (0,0), (-1,-1), 14),
        ("BOX",        (0,0), (-1,-1), 1, TEAL),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph("Generated by AuraAI  ·  Study Notes & Q&A", sub_style))
    story.append(HRFlowable(width="100%", thickness=1, color=TEAL, spaceAfter=10))

    if data.get("summary"):
        story.append(Paragraph("Overview", section_style))
        story.append(Paragraph(data["summary"], body_style))

    if data.get("key_points"):
        story.append(Paragraph("Key Points", section_style))
        for pt in data["key_points"]:
            story.append(Paragraph(f"• {pt}", bullet_style))

    story.append(Spacer(1, 0.4*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=SLATE, spaceAfter=8))

    questions = data.get("questions", [])
    if questions:
        story.append(Paragraph("Questions & Answers", section_style))
        for i, item in enumerate(questions, 1):
            qtype = item.get("type", "short").upper()
            badge_color = {
                "SHORT": colors.HexColor("#00d4aa"),
                "LONG":  colors.HexColor("#ff6b35"),
                "TRUE/FALSE": colors.HexColor("#6c63ff"),
                "FILL-IN-THE-BLANK": colors.HexColor("#f59e0b"),
            }.get(qtype, MUTED)

            badge_para = Paragraph(qtype, badge_style)
            badge_cell = Table([[badge_para]], colWidths=[2.8*cm])
            badge_cell.setStyle(TableStyle([
                ("BACKGROUND", (0,0), (-1,-1), badge_color),
                ("ROWPADDING", (0,0), (-1,-1), 3),
            ]))

            q_para = Paragraph(f"<b>Q{i}.</b> {item.get('q', '')}", q_style)
            row = Table([[badge_cell, q_para]], colWidths=[3*cm, 14*cm])
            row.setStyle(TableStyle([
                ("VALIGN",      (0,0), (-1,-1), "TOP"),
                ("LEFTPADDING", (0,0), (0,-1), 0),
                ("TOPPADDING",  (0,0), (-1,-1), 2),
            ]))
            story.append(row)
            story.append(Paragraph(f"<font color='#8896ae'>Answer: </font>{item.get('a', '')}", a_style))

            if i < len(questions):
                story.append(HRFlowable(width="100%", thickness=0.3,
                    color=SLATE, spaceAfter=4, spaceBefore=4))

    story.append(Spacer(1, 0.6*cm))
    story.append(HRFlowable(width="100%", thickness=1, color=TEAL, spaceAfter=6))
    story.append(Paragraph("AuraAI Study Notes  ·  Free AI-Powered Learning Tool", sub_style))

    doc.build(story)
    buf.seek(0)
    return buf.read()


# ════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════

def get_db():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL missing")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        id SERIAL PRIMARY KEY, username TEXT UNIQUE,
        password TEXT, bio TEXT DEFAULT '',
        avatar_url TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS conversations(
        id SERIAL PRIMARY KEY, username TEXT, title TEXT,
        mode TEXT DEFAULT 'chat', pinned BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS chats(
        id SERIAL PRIMARY KEY, conversation_id INTEGER, username TEXT,
        user_msg TEXT, bot_msg TEXT, bot_plain TEXT, type TEXT DEFAULT 'text',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS notes(
        id SERIAL PRIMARY KEY, username TEXT NOT NULL, title TEXT,
        source_text TEXT, notes_json TEXT, quiz_json TEXT, flashcards_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS stories(
        id SERIAL PRIMARY KEY, username TEXT NOT NULL, content TEXT,
        media_url TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS story_views(
        id SERIAL PRIMARY KEY, story_id INTEGER, viewer TEXT,
        viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS bookmarks(
        id SERIAL PRIMARY KEY, username TEXT NOT NULL, chat_id INTEGER,
        note TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS reminders(
        id SERIAL PRIMARY KEY, username TEXT NOT NULL, title TEXT,
        body TEXT, remind_at TIMESTAMP, done BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    # Add missing columns if they don't exist
    try:
        c.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS bot_plain TEXT")
        c.execute("ALTER TABLE notes ADD COLUMN IF NOT EXISTS quiz_json TEXT")
        c.execute("ALTER TABLE notes ADD COLUMN IF NOT EXISTS flashcards_json TEXT")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT DEFAULT ''")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT DEFAULT ''")
        c.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pinned BOOLEAN DEFAULT FALSE")
    except:
        pass
    conn.commit()
    conn.close()


# ════════════════════════════════════════════
#  ROUTES — AUTH
# ════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        user = request.form.get("username", "").strip()
        pwd  = request.form.get("password", "")
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT password FROM users WHERE username=%s", (user,))
        row = c.fetchone()
        if row:
            stored = row[0]
            valid = bcrypt.checkpw(pwd.encode(), stored.encode()) if stored.startswith("$2b$") else pwd == stored
            if valid:
                session["user"] = user
                conn.close()
                return redirect("/")
        conn.close()
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        user = request.form.get("username", "").strip()
        pwd  = request.form.get("password", "")
        if len(user) < 3:
            error = "Username must be at least 3 characters."
        elif len(pwd) < 4:
            error = "Password must be at least 4 characters."
        else:
            try:
                hashed = bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()
                conn = get_db(); c = conn.cursor()
                c.execute("INSERT INTO users (username, password) VALUES (%s, %s)", (user, hashed))
                conn.commit(); conn.close()
                return redirect("/login")
            except Exception:
                error = "Username already exists."
    return render_template("register.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ════════════════════════════════════════════
#  ROUTES — MAIN PAGES
# ════════════════════════════════════════════

@app.route("/")
def home():
    if "user" not in session:
        return redirect("/login")

    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, title, mode, pinned FROM conversations WHERE username=%s ORDER BY pinned DESC, id DESC", (session["user"],))
        convs = c.fetchall()
        c.execute("SELECT id, title, created_at FROM notes WHERE username=%s ORDER BY id DESC LIMIT 20", (session["user"],))
        notes_list = c.fetchall()
        c.execute("SELECT id, title, remind_at FROM reminders WHERE username=%s AND done=FALSE AND remind_at > NOW() ORDER BY remind_at LIMIT 5", (session["user"],))
        reminders = c.fetchall()
        conn.close()
    except Exception as e:
        print("DASHBOARD ERROR:", e)
        convs = []; notes_list = []; reminders = []

    return render_template("dashboard.html", chat=[], conversations=convs,
                           active_chat=None, notes_list=notes_list, reminders=reminders,
                           username=session["user"])


@app.route("/new_chat")
def new_chat():
    session.pop("conv_id", None)
    return redirect("/")


@app.route("/chat/<int:conv_id>")
def open_chat(conv_id):
    if "user" not in session:
        return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id FROM conversations WHERE id=%s AND username=%s", (conv_id, session["user"]))
    if not c.fetchone():
        conn.close(); return redirect("/")
    session["conv_id"] = conv_id
    c.execute("SELECT user_msg, bot_msg, type FROM chats WHERE conversation_id=%s ORDER BY id", (conv_id,))
    rows = c.fetchall()
    c.execute("SELECT id, title, mode, pinned FROM conversations WHERE username=%s ORDER BY pinned DESC, id DESC", (session["user"],))
    convs = c.fetchall()
    c.execute("SELECT id, title, created_at FROM notes WHERE username=%s ORDER BY id DESC LIMIT 20", (session["user"],))
    notes_list = c.fetchall()
    c.execute("SELECT id, title, remind_at FROM reminders WHERE username=%s AND done=FALSE AND remind_at > NOW() ORDER BY remind_at LIMIT 5", (session["user"],))
    reminders = c.fetchall()
    conn.close()
    chat = [{"type": r[2], "user": r[0],
             "bot":   r[1] if r[2] == "text"  else None,
             "image": r[1] if r[2] == "image" else None} for r in rows]
    return render_template("dashboard.html", chat=chat, conversations=convs,
                           active_chat=conv_id, notes_list=notes_list, reminders=reminders,
                           username=session["user"])


# ════════════════════════════════════════════
#  ROUTES — CHAT
# ════════════════════════════════════════════

@app.route("/tool", methods=["POST"])
def tool():
    if "user" not in session:
        return redirect("/login")

    try:
        user_input = request.form.get("input", "").strip()
        mode = request.form.get("mode", "chat")

        # Handle image generation
        lower = user_input.lower()
        if any(lower.startswith(p) for p in ["generate image:", "create image:", "draw:", "image:", "/image "]):
            prompt = re.sub(r'^(generate image:|create image:|draw:|image:|/image )', '', user_input, flags=re.IGNORECASE).strip()
            img_url = generate_image_url(prompt)

            conn = get_db(); c = conn.cursor()
            conv_id = session.get("conv_id")
            if not conv_id:
                title = generate_chat_title(user_input)
                c.execute("INSERT INTO conversations (username, title) VALUES (%s,%s) RETURNING id",
                          (session["user"], title))
                conv_id = c.fetchone()[0]
                session["conv_id"] = conv_id

            c.execute("INSERT INTO chats (conversation_id,username,user_msg,bot_msg,type) VALUES (%s,%s,%s,%s,'image')",
                      (conv_id, session["user"], user_input, img_url))
            conn.commit(); conn.close()
            return redirect(f"/chat/{conv_id}")

        conn = get_db()
        c = conn.cursor()
        conv_id = session.get("conv_id")

        if not conv_id:
            title = generate_chat_title(user_input or "New Chat")
            c.execute("INSERT INTO conversations (username, title, mode) VALUES (%s,%s,%s) RETURNING id",
                      (session["user"], title, mode))
            conv_id = c.fetchone()[0]
            session["conv_id"] = conv_id
            conn.commit()

        session["pending_prompt"] = user_input
        session["pending_mode"] = mode
        conn.close()
        return redirect(f"/chat/{conv_id}?do_stream=true")

    except Exception as e:
        print("TOOL ERROR:", e)
        return "Tool Error", 500


@app.route("/stream")
def stream():
    if "user" not in session:
        return Response("Unauthorized", status=401)

    prompt   = session.pop("pending_prompt", "Hello")
    mode     = session.pop("pending_mode", "chat")
    conv_id  = session.get("conv_id")
    username = session["user"]

    # Pick system prompt based on mode
    system_map = {
        "code":    CODE_SYSTEM,
        "explain": EXPLAIN_SYSTEM,
        "default": "You are AuraAI, a helpful assistant. Answer clearly and thoroughly."
    }
    system = system_map.get(mode, system_map["default"])

    def generate():
        if not conv_id:
            yield "data: Conversation error\n\n"; return

        # Get recent history for context
        history = []
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("SELECT user_msg, bot_plain FROM chats WHERE conversation_id=%s ORDER BY id DESC LIMIT 10", (conv_id,))
            rows = c.fetchall()
            conn.close()
            history = [{"user": r[0], "bot_plain": r[1]} for r in reversed(rows)]
        except:
            pass

        full = []
        try:
            for chunk in query_ai_stream(prompt, system=system, history=history):
                full.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            complete = "".join(full)
            try:
                formatted = markdown.markdown(complete)
            except:
                formatted = complete

            try:
                conn = get_db(); c = conn.cursor()
                c.execute("""INSERT INTO chats 
                    (conversation_id,username,user_msg,bot_msg,bot_plain,type)
                    VALUES (%s,%s,%s,%s,%s,'text')""",
                    (conv_id, username, prompt, formatted, complete))
                conn.commit(); conn.close()
            except Exception as e:
                print("DB ERROR:", e)

            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: Error: {str(e)}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ════════════════════════════════════════════
#  ROUTES — AI TOOLS (new features)
# ════════════════════════════════════════════

@app.route("/api/summarize", methods=["POST"])
def api_summarize():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text"}), 400
    raw = query_ai(f"Summarize this:\n\n{text[:5000]}", system=SUMMARIZE_SYSTEM, large=True)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"```$", "", raw).strip()
    try:
        return jsonify({"ok": True, "data": json.loads(raw)})
    except:
        return jsonify({"ok": True, "data": {"tldr": raw[:200], "bullets": [], "full_summary": raw}})


@app.route("/api/grammar", methods=["POST"])
def api_grammar():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text"}), 400
    raw = query_ai(f"Check and correct:\n\n{text[:3000]}", system=GRAMMAR_SYSTEM)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"```$", "", raw).strip()
    try:
        return jsonify({"ok": True, "data": json.loads(raw)})
    except:
        return jsonify({"ok": True, "data": {"corrected": raw, "issues": [], "score": 0}})


@app.route("/api/translate", methods=["POST"])
def api_translate():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    text = body.get("text", "").strip()
    lang = body.get("language", "Spanish")
    if not text:
        return jsonify({"error": "No text"}), 400
    result = query_ai(f"Translate to {lang}:\n\n{text[:3000]}", system=TRANSLATE_SYSTEM)
    return jsonify({"ok": True, "translation": result})


@app.route("/api/explain", methods=["POST"])
def api_explain():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    topic = body.get("topic", "").strip()
    level = body.get("level", "intermediate")
    if not topic:
        return jsonify({"error": "No topic"}), 400
    result = query_ai(
        f"Explain '{topic}' at a {level} level with examples and analogies.",
        system=EXPLAIN_SYSTEM, large=True
    )
    return jsonify({"ok": True, "explanation": markdown.markdown(result)})


@app.route("/api/code", methods=["POST"])
def api_code():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "No prompt"}), 400
    result = query_ai(prompt, system=CODE_SYSTEM, large=True)
    return jsonify({"ok": True, "result": markdown.markdown(result)})


@app.route("/api/quiz", methods=["POST"])
def api_quiz():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    text = body.get("text", "").strip()
    note_id = body.get("note_id")
    if note_id:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT source_text, quiz_json FROM notes WHERE id=%s AND username=%s", (note_id, session["user"]))
        row = c.fetchone()
        conn.close()
        if row:
            if row[1]:
                return jsonify({"ok": True, "questions": json.loads(row[1])})
            text = row[0] or ""
    if not text:
        return jsonify({"error": "No content"}), 400
    questions = generate_quiz(text)
    if note_id and questions:
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("UPDATE notes SET quiz_json=%s WHERE id=%s", (json.dumps(questions), note_id))
            conn.commit(); conn.close()
        except: pass
    return jsonify({"ok": True, "questions": questions})


@app.route("/api/flashcards", methods=["POST"])
def api_flashcards():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    text = body.get("text", "").strip()
    note_id = body.get("note_id")
    if note_id:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT source_text, flashcards_json FROM notes WHERE id=%s AND username=%s", (note_id, session["user"]))
        row = c.fetchone()
        conn.close()
        if row:
            if row[1]:
                return jsonify({"ok": True, "cards": json.loads(row[1])})
            text = row[0] or ""
    if not text:
        return jsonify({"error": "No content"}), 400
    cards = generate_flashcards(text)
    if note_id and cards:
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("UPDATE notes SET flashcards_json=%s WHERE id=%s", (json.dumps(cards), note_id))
            conn.commit(); conn.close()
        except: pass
    return jsonify({"ok": True, "cards": cards})


# ════════════════════════════════════════════
#  ROUTES — BOOKMARKS
# ════════════════════════════════════════════

@app.route("/api/bookmark", methods=["POST"])
def add_bookmark():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    chat_id = body.get("chat_id")
    note = body.get("note", "")
    if not chat_id:
        return jsonify({"error": "No chat_id"}), 400
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO bookmarks (username, chat_id, note) VALUES (%s,%s,%s)", (session["user"], chat_id, note))
    conn.commit(); conn.close()
    return jsonify({"ok": True})


@app.route("/bookmarks")
def bookmarks_page():
    if "user" not in session:
        return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("""SELECT b.id, b.note, b.created_at, ch.user_msg, ch.bot_msg, ch.conversation_id
                 FROM bookmarks b JOIN chats ch ON b.chat_id=ch.id
                 WHERE b.username=%s ORDER BY b.id DESC""", (session["user"],))
    bms = c.fetchall()
    c.execute("SELECT id, title, mode, pinned FROM conversations WHERE username=%s ORDER BY pinned DESC, id DESC", (session["user"],))
    convs = c.fetchall()
    c.execute("SELECT id, title, created_at FROM notes WHERE username=%s ORDER BY id DESC LIMIT 20", (session["user"],))
    notes_list = c.fetchall()
    conn.close()
    return render_template("bookmarks.html", bookmarks=bms, conversations=convs, notes_list=notes_list)


@app.route("/api/bookmark/<int:bm_id>/delete", methods=["POST"])
def delete_bookmark(bm_id):
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM bookmarks WHERE id=%s AND username=%s", (bm_id, session["user"]))
    conn.commit(); conn.close()
    return jsonify({"ok": True})


# ════════════════════════════════════════════
#  ROUTES — REMINDERS
# ════════════════════════════════════════════

@app.route("/api/reminders", methods=["GET"])
def get_reminders():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id, title, body, remind_at, done FROM reminders WHERE username=%s ORDER BY remind_at", (session["user"],))
    rows = c.fetchall()
    conn.close()
    return jsonify({"reminders": [{"id": r[0], "title": r[1], "body": r[2],
                                    "remind_at": str(r[3]), "done": r[4]} for r in rows]})


@app.route("/api/reminders", methods=["POST"])
def add_reminder():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    title = body.get("title", "").strip()
    remind_at = body.get("remind_at", "")
    rb = body.get("body", "")
    if not title or not remind_at:
        return jsonify({"error": "Title and time required"}), 400
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO reminders (username, title, body, remind_at) VALUES (%s,%s,%s,%s) RETURNING id",
              (session["user"], title, rb, remind_at))
    rid = c.fetchone()[0]
    conn.commit(); conn.close()
    return jsonify({"ok": True, "id": rid})


@app.route("/api/reminders/<int:rid>/done", methods=["POST"])
def done_reminder(rid):
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE reminders SET done=TRUE WHERE id=%s AND username=%s", (rid, session["user"]))
    conn.commit(); conn.close()
    return jsonify({"ok": True})


# ════════════════════════════════════════════
#  ROUTES — NOTES
# ════════════════════════════════════════════

@app.route("/notes")
def notes_page():
    if "user" not in session:
        return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id, title, created_at FROM notes WHERE username=%s ORDER BY id DESC", (session["user"],))
    notes_list = c.fetchall()
    c.execute("SELECT id, title, mode, pinned FROM conversations WHERE username=%s ORDER BY pinned DESC, id DESC", (session["user"],))
    convs = c.fetchall()
    conn.close()
    return render_template("notes.html", notes_list=notes_list, conversations=convs)


@app.route("/generate_notes", methods=["POST"])
def generate_notes():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    file  = request.files.get("file")
    text  = request.form.get("text", "").strip()
    extra = request.form.get("instruction", "").strip()

    source_text = text

    if file and file.filename:
        fname = file.filename.lower()
        if fname.endswith(".pdf"):
            try:
                with pdfplumber.open(file) as pdf:
                    for page in pdf.pages:
                        source_text += (page.extract_text() or "")
            except Exception as e:
                return jsonify({"error": f"PDF read failed: {e}"}), 400
        elif fname.endswith(".txt") or fname.endswith(".md"):
            try:
                source_text += file.read().decode("utf-8", errors="ignore")
            except Exception as e:
                return jsonify({"error": f"File read failed: {e}"}), 400
        elif OCR_AVAILABLE and any(fname.endswith(x) for x in [".png",".jpg",".jpeg",".webp",".bmp"]):
            try:
                img = Image.open(file)
                source_text += pytesseract.image_to_string(img)
            except Exception as e:
                return jsonify({"error": f"Image OCR failed: {e}"}), 400
        else:
            return jsonify({"error": "Unsupported file type. Use PDF, TXT, or image."}), 400

    if not source_text:
        return jsonify({"error": "No content provided. Upload a file or paste text."}), 400

    notes_data = generate_notes_from_text(source_text, extra)

    conn = get_db(); c = conn.cursor()
    c.execute("""INSERT INTO notes (username, title, source_text, notes_json)
                 VALUES (%s, %s, %s, %s) RETURNING id""",
              (session["user"], notes_data.get("title","Notes"),
               source_text[:4000], json.dumps(notes_data)))
    note_id = c.fetchone()[0]
    conn.commit(); conn.close()

    return jsonify({"ok": True, "note_id": note_id, "data": notes_data})


@app.route("/notes/<int:note_id>")
def view_note(note_id):
    if "user" not in session:
        return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT title, notes_json, created_at, source_text FROM notes WHERE id=%s AND username=%s",
              (note_id, session["user"]))
    row = c.fetchone()
    conn.close()
    if not row:
        return redirect("/notes")
    data = json.loads(row[1])
    has_source = bool(row[3])
    return render_template("note_view.html", note=data, note_id=note_id,
                           created_at=row[2], has_source=has_source)


@app.route("/notes/<int:note_id>/pdf")
def download_note_pdf(note_id):
    if "user" not in session:
        return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT title, notes_json FROM notes WHERE id=%s AND username=%s",
              (note_id, session["user"]))
    row = c.fetchone()
    conn.close()
    if not row:
        return "Note not found", 404
    data = json.loads(row[1])
    pdf_bytes = build_notes_pdf(data)
    safe_title = re.sub(r"[^\w\s-]", "", data.get("title", "notes"))[:50].replace(" ", "_")
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="AuraAI_{safe_title}.pdf"'})


@app.route("/notes/<int:note_id>/delete", methods=["POST"])
def delete_note(note_id):
    if "user" not in session:
        return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM notes WHERE id=%s AND username=%s", (note_id, session["user"]))
    conn.commit(); conn.close()
    return redirect("/notes")


# ════════════════════════════════════════════
#  ROUTES — PROFILE
# ════════════════════════════════════════════

@app.route("/profile")
def profile():
    if "user" not in session:
        return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT username, bio, avatar_url, created_at FROM users WHERE username=%s", (session["user"],))
    user = c.fetchone()
    c.execute("SELECT COUNT(*) FROM conversations WHERE username=%s", (session["user"],))
    chat_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM notes WHERE username=%s", (session["user"],))
    note_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM chats WHERE username=%s", (session["user"],))
    msg_count = c.fetchone()[0]
    c.execute("SELECT id, title, mode, pinned FROM conversations WHERE username=%s ORDER BY pinned DESC, id DESC", (session["user"],))
    convs = c.fetchall()
    c.execute("SELECT id, title, created_at FROM notes WHERE username=%s ORDER BY id DESC LIMIT 20", (session["user"],))
    notes_list = c.fetchall()
    conn.close()
    return render_template("profile.html", user=user, chat_count=chat_count,
                           note_count=note_count, msg_count=msg_count,
                           conversations=convs, notes_list=notes_list)


@app.route("/profile/update", methods=["POST"])
def update_profile():
    if "user" not in session:
        return redirect("/login")
    bio = request.form.get("bio", "")[:300]
    avatar_url = request.form.get("avatar_url", "")[:500]
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE users SET bio=%s, avatar_url=%s WHERE username=%s",
              (bio, avatar_url, session["user"]))
    conn.commit(); conn.close()
    return redirect("/profile")


@app.route("/profile/change_password", methods=["POST"])
def change_password():
    if "user" not in session:
        return redirect("/login")
    old = request.form.get("old_password", "")
    new = request.form.get("new_password", "")
    if len(new) < 4:
        return redirect("/profile?error=Password too short")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT password FROM users WHERE username=%s", (session["user"],))
    row = c.fetchone()
    if not row:
        conn.close(); return redirect("/profile")
    stored = row[0]
    valid = bcrypt.checkpw(old.encode(), stored.encode()) if stored.startswith("$2b$") else old == stored
    if not valid:
        conn.close(); return redirect("/profile?error=Wrong current password")
    hashed = bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode()
    c.execute("UPDATE users SET password=%s WHERE username=%s", (hashed, session["user"]))
    conn.commit(); conn.close()
    return redirect("/profile?success=Password changed")


# ════════════════════════════════════════════
#  ROUTES — SEARCH / STORIES / CHAT MGMT
# ════════════════════════════════════════════

@app.route("/search")
def search():
    if "user" not in session:
        return jsonify({"results": []})
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"results": []})
    conn = get_db(); c = conn.cursor()
    c.execute("""SELECT id, conversation_id, user_msg, bot_msg FROM chats
                 WHERE username=%s AND (user_msg ILIKE %s OR bot_msg ILIKE %s)
                 ORDER BY id DESC LIMIT 20""",
              (session["user"], f"%{q}%", f"%{q}%"))
    rows = c.fetchall(); conn.close()
    results = []
    for r in rows:
        full = (r[2] or "") + " " + (r[3] or "")
        idx  = full.lower().find(q.lower())
        snippet = full[max(0, idx-40): idx+60]
        results.append({"conv_id": r[1], "msg_id": r[0], "snippet": snippet})
    return jsonify({"results": results})


@app.route("/add_story", methods=["POST"])
def add_story():
    if "user" not in session:
        return redirect("/login")
    content   = request.form.get("content", "")
    media_url = request.form.get("media_url", "")
    conn = get_db(); c = conn.cursor()
    c.execute("""INSERT INTO stories (username, content, media_url, expires_at)
                 VALUES (%s, %s, %s, NOW() + INTERVAL '24 HOURS')""",
              (session["user"], content, media_url))
    conn.commit(); conn.close()
    return redirect("/")


@app.route("/stories")
def get_stories():
    if "user" not in session:
        return jsonify([])
    conn = get_db(); c = conn.cursor()
    c.execute("""SELECT id, username, content, media_url, created_at
                 FROM stories WHERE expires_at > NOW() ORDER BY created_at DESC""")
    rows = c.fetchall(); conn.close()
    return jsonify([{"id": r[0], "user": r[1], "content": r[2],
                     "media": r[3], "created_at": str(r[4])} for r in rows])


@app.route("/view_story/<int:story_id>")
def view_story(story_id):
    if "user" not in session:
        return jsonify({"status": "error"})
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO story_views (story_id, viewer) VALUES (%s,%s)", (story_id, session["user"]))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})


@app.route("/delete_chat/<int:conv_id>", methods=["POST"])
def delete_chat(conv_id):
    if "user" not in session:
        return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM chats WHERE conversation_id=%s AND username=%s", (conv_id, session["user"]))
    c.execute("DELETE FROM conversations WHERE id=%s AND username=%s", (conv_id, session["user"]))
    conn.commit(); conn.close()
    if session.get("conv_id") == conv_id:
        session.pop("conv_id", None)
    return redirect("/")


@app.route("/edit_chat/<int:conv_id>", methods=["POST"])
def edit_chat(conv_id):
    if "user" not in session:
        return redirect("/login")
    title = request.form.get("title", "").strip()
    if title:
        conn = get_db(); c = conn.cursor()
        c.execute("UPDATE conversations SET title=%s WHERE id=%s AND username=%s",
                  (title, conv_id, session["user"]))
        conn.commit(); conn.close()
    return redirect("/")


@app.route("/pin_chat/<int:conv_id>", methods=["POST"])
def pin_chat(conv_id):
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT pinned FROM conversations WHERE id=%s AND username=%s", (conv_id, session["user"]))
    row = c.fetchone()
    if row:
        c.execute("UPDATE conversations SET pinned=%s WHERE id=%s AND username=%s",
                  (not row[0], conv_id, session["user"]))
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/quick_pdf", methods=["POST"])
def quick_pdf():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    notes_data = body.get("notes_data", {})
    if not notes_data:
        return jsonify({"error": "No data"}), 400
    pdf_bytes = build_notes_pdf(notes_data)
    safe = re.sub(r"[^\w\s-]", "", notes_data.get("title","notes"))[:40].replace(" ", "_")
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="AuraAI_{safe}.pdf"'})


@app.route("/export_chat/<int:conv_id>")
def export_chat(conv_id):
    """Export conversation as plain text."""
    if "user" not in session:
        return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT title FROM conversations WHERE id=%s AND username=%s", (conv_id, session["user"]))
    conv = c.fetchone()
    if not conv:
        conn.close(); return redirect("/")
    c.execute("SELECT user_msg, bot_plain, created_at FROM chats WHERE conversation_id=%s ORDER BY id", (conv_id,))
    rows = c.fetchall()
    conn.close()
    lines = [f"AuraAI Chat Export: {conv[0]}", "="*50, ""]
    for r in rows:
        lines.append(f"You: {r[0]}")
        lines.append(f"AuraAI: {r[1] or '(image)'}")
        lines.append(f"[{r[2]}]")
        lines.append("")
    content = "\n".join(lines)
    safe = re.sub(r"[^\w\s-]", "", conv[0])[:40].replace(" ", "_")
    return Response(content, mimetype="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="AuraAI_{safe}.txt"'})


# ════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)