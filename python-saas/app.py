"""
NexusAI — Premium AI Platform
Tiered subscription system: Free / Pro / Elite
"""

from flask import (
    Flask, request, render_template, redirect,
    session, jsonify, Response, stream_with_context, url_for
)
import psycopg2
import os, io, re, json, time, hashlib, hmac, secrets
import markdown
import bcrypt
import pdfplumber
from datetime import datetime, timedelta
from dotenv import load_dotenv
import redis

# ── PDF generation ──
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY

# ── AI SDKs ──
from openai import OpenAI            # OpenAI + Groq (free/pro chat)
import anthropic                     # Claude (pro/elite code)
import google.generativeai as genai  # Gemini (elite images/video)

# ── Payments ──
import stripe

# ── Email / SMS ──
# from sendgrid import SendGridAPIClient   # email verification
# from twilio.rest import Client           # SMS verification

# ── Optional OCR ──
try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

load_dotenv()

app = Flask(__name__)
# FIX #9: Warn if SECRET_KEY is missing — random key invalidates all sessions on restart
_secret = os.getenv("SECRET_KEY")
if not _secret:
    import warnings
    warnings.warn(
        "SECRET_KEY env var not set. A random key is being used — all sessions will be "
        "lost on every restart. Set SECRET_KEY in your .env file.",
        RuntimeWarning, stacklevel=1)
    _secret = secrets.token_hex(32)
app.secret_key = _secret

# ═══════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Initialize Redis client
try:
    redis_client = redis.from_url(REDIS_URL)
    redis_client.ping()
    print("[STARTUP] Connected to Redis successfully.")
except Exception as e:
    print(f"[STARTUP] Redis connection failed, bypassing cache: {e}")
    redis_client = None

# Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_BASIC_PRICE_ID  = os.getenv("STRIPE_BASIC_PRICE_ID")   # $9.99/mo
STRIPE_MEDIUM_PRICE_ID = os.getenv("STRIPE_MEDIUM_PRICE_ID")  # $29.99/mo
STRIPE_ELITE_PRICE_ID  = os.getenv("STRIPE_ELITE_PRICE_ID")   # $99.99/mo

# Plans — 4 tiers with daily credit budgets
# ── API cost reference (per call, used to derive credit costs) ──
# Groq/Llama:     ~$0.00005/call   (near free)
# GPT-4o-mini:    ~$0.001/call Claude Haiku:   ~$0.002/call
# Claude Sonnet:  ~$0.036/call
# Claude Opus:    ~$0.180/call
# DALL-E 3:       ~$0.040/image
# Gemini image:   ~$0.040/image
# Gemini video:   ~$0.100/clip
#
# Pricing margin target: 50% gross margin at 40% avg daily utilisation.
# Formula: credits_needed = api_cost / (plan_revenue_per_credit * 0.4 * 0.5)

PLANS = {
    "free": {
        "name": "Free",
        "price": 0,
        "chat_model": "gemini-1.5-flash",         # Google — High quality, free tier
        "code_model": "gemini-1.5-flash",
        "image_provider": "gemini",               # Upgraded to Gemini
        "credits_per_day": 50,                    # Reduced from 100
        "features": ["basic_chat", "basic_notes", "grammar", "translate"],
        "badge": "Free",
        "color": "#6b7280",
    },
    "basic": {
        "name": "Basic",
        "price": 9.99,
        "chat_model": "gpt-4o-mini",              # $0.001/call
        "code_model": "claude-3-haiku-20240307",  # $0.002/call
        "image_provider": "dalle3",               # $0.040/image
        "credits_per_day": 1000,                  # Reduced from 1500
        "features": ["basic_chat", "basic_notes", "grammar", "translate",
                     "advanced_chat", "code_assist", "dalle_images",
                     "quiz", "flashcards", "summarize", "explain", "priority_support", "video_gen"],
        "badge": "Basic",
        "color": "#3b82f6",
    },
    "medium": {
        "name": "Medium",
        "price": 29.99,
        "chat_model": "gpt-4o",                      # $0.006/call
        "code_model": "claude-3-5-sonnet-20241022",  # $0.036/call
        "image_provider": "dalle3",                  # $0.040/image
        "credits_per_day": 4000,                  # Reduced from 6000
        "features": ["basic_chat", "basic_notes", "grammar", "translate",
                     "advanced_chat", "code_assist", "dalle_images",
                     "quiz", "flashcards", "summarize", "explain",
                     "voice_clone", "priority_support", "video_analysis", "video_gen"],
        "badge": "Medium",
        "color": "#8b5cf6",
    },
    "elite": {
        "name": "Elite",
        "price": 99.99,
        "chat_model": "gpt-4o",                                     # $0.006/call
        "code_model": "claude-opus-4-5",                            # $0.180/call
        "image_model": "gemini-2.0-flash-preview-image-generation", # $0.040/image
        "video_model": "gemini-2.5-flash",                          # $0.100/clip
        "image_provider": "gemini",
        "credits_per_day": 15000,                 # Reduced from 25000
        "features": ["*"],
        "badge": "Elite",
        "color": "#f59e0b",
    }
}

# Credits deducted per action, now dynamically priced by plan!
# Free   (Llama/Groq)   -> near free, but charge something
# Basic  (GPT-4o-mini)  -> cheap, chat=6
# Medium (GPT-4o/Sonnet)-> expensive, chat=50, code=250
# Elite  (Opus/Gemini)  -> very expensive, code=1500, video=1500
PLAN_CREDIT_COSTS = {
    "free": {
        "chat": 1, "grammar": 1, "translate": 1, "summarize": 2, "explain": 2,
        "code": 2, "notes": 5, "quiz": 3, "flashcards": 3, "image": 25,
    },
    "basic": {
        "chat": 7, "grammar": 5, "translate": 5, "summarize": 15, "explain": 15,
        "code": 10, "notes": 30, "quiz": 20, "flashcards": 20, "image": 150, "video": 800,
    },
    "medium": {
        "chat": 60, "grammar": 40, "translate": 40, "summarize": 80, "explain": 80,
        "code": 300, "notes": 120, "quiz": 60, "flashcards": 60, "image": 300, "video": 1000,
    },
    "elite": {
        "chat": 70, "grammar": 50, "translate": 50, "summarize": 100, "explain": 100,
        "code": 2000, "notes": 150, "quiz": 80, "flashcards": 80, "image": 450, "video": 2000,
    }
}
# GPT-4o:         ~$0.006/call
#

# Safety Check: Only initialize if keys exist to prevent startup crash
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_KEY = os.getenv("GOOGLE_AI_API_KEY")

openai_client = OpenAI(api_key=OPENAI_KEY or "dummy") if OPENAI_KEY else None
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY or "dummy") if ANTHROPIC_KEY else None

if GOOGLE_KEY:
    genai.configure(api_key=GOOGLE_KEY)
else:
    print("[STARTUP] Warning: GOOGLE_AI_API_KEY missing.")

# ════════════════════════════════════════════
#  PLAN HELPERS
# ════════════════════════════════════════════

def get_user_plan(username):
    if redis_client:
        try:
            cached = redis_client.get(f"plan:{username}")
            if cached: return cached.decode("utf-8")
        except: pass

    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT plan, plan_expires_at FROM users WHERE username=%s", (username,))
        row = c.fetchone(); conn.close()
        if not row: return "free"
        plan, expires = row[0] or "free", row[1]
        if plan != "free" and expires and expires < datetime.utcnow():
            # expired — downgrade
            conn2 = get_db(); c2 = conn2.cursor()
            c2.execute("UPDATE users SET plan='free' WHERE username=%s", (username,))
            conn2.commit(); conn2.close()
            plan = "free"
        
        if redis_client:
            try: redis_client.setex(f"plan:{username}", 300, plan) # Cache for 5 mins
            except: pass
        return plan
    except:
        return "free"

def plan_has_feature(plan_name, feature):
    p = PLANS.get(plan_name, PLANS["free"])
    feats = p["features"]
    return "*" in feats or feature in feats

def check_credits(username, action="chat"):
    """Returns (credits_used_today, daily_limit, ok).
    ok=True means user has enough credits for the action."""
    plan = get_user_plan(username)
    if action not in PLAN_CREDIT_COSTS.get(plan, {}):
        raise ValueError(f"Unknown action: {action!r}")
    
    p = PLANS[plan]
    daily_limit = p["credits_per_day"]
    cost = PLAN_CREDIT_COSTS[plan][action]
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""SELECT COALESCE(SUM(tokens_used), 0) FROM usage_logs
                     WHERE username=%s AND created_at > NOW() - INTERVAL '24 hours'""", (username,))
        used = int(c.fetchone()[0]); conn.close()
        return used, daily_limit, (used + cost) <= daily_limit
    except Exception as e:
        print(f"[check_credits] DB error: {e}")
        return 0, daily_limit, True


def spend_credits(username, plan, action, model=""):
    """Insert a usage_log row deducting PLAN_CREDIT_COSTS[plan][action] from daily budget."""
    cost = PLAN_CREDIT_COSTS.get(plan, {}).get(action, 1)
    try:
        conn = get_db(); c = conn.cursor()
        c.execute(
            "INSERT INTO usage_logs (username, action, plan, model, tokens_used) VALUES (%s,%s,%s,%s,%s)",
            (username, action, plan, model, cost))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[spend_credits] DB error: {e}")


# Legacy alias kept for any remaining call-sites
def check_daily_limit(username, limit_type):
    action = "chat" if limit_type == "msgs" else "notes"
    return check_credits(username, action)

def get_chat_model(plan_name):
    return PLANS.get(plan_name, PLANS["free"])["chat_model"]

def get_code_model(plan_name):
    return PLANS.get(plan_name, PLANS["free"])["code_model"]

def get_image_provider(plan_name):
    return PLANS.get(plan_name, PLANS["free"])["image_provider"]

# ════════════════════════════════════════════
#  AI ROUTER — routes to correct API by plan
# ════════════════════════════════════════════

def ai_chat_stream(prompt, system, history, plan):
    """Stream chat response using plan-appropriate model."""
    model = get_chat_model(plan)

    if plan == "free":
        # Primary: Google Gemini 1.5 Flash
        try:
            m = genai.GenerativeModel(model)
            chat_session = m.start_chat(history=[])
            for h in (history or [])[-8:]:
                chat_session.history.append({"role": "user", "parts": [h["user"]]})
                chat_session.history.append({"role": "model", "parts": [h["bot_plain"] or ""]})
            resp = chat_session.send_message(f"{system}\n\n{prompt}", stream=True)
            for chunk in resp:
                if chunk.text: yield chunk.text
        except Exception as e:
            yield f"\n\n*Error: Gemini failed: {e}*"

    elif plan == "basic":
        # OpenAI GPT-4o-mini
        messages = [{"role": "system", "content": system}]
        for h in (history or [])[-12:]:
            messages.append({"role": "user", "content": h["user"]})
            messages.append({"role": "assistant", "content": h["bot_plain"] or ""})
        messages.append({"role": "user", "content": prompt})
        try:
            resp = openai_client.chat.completions.create(
                model=model, messages=messages, max_tokens=2048, stream=True)
            for chunk in resp:
                delta = chunk.choices[0].delta.content
                if delta: yield delta
        except Exception as e:
            yield f"\n\n*Error: {e}*"

    elif plan in ("medium", "elite"):
        # OpenAI GPT-4o — highest quality
        messages = [{"role": "system", "content": system}]
        for h in (history or [])[-20:]:
            messages.append({"role": "user", "content": h["user"]})
            messages.append({"role": "assistant", "content": h["bot_plain"] or ""})
        messages.append({"role": "user", "content": prompt})
        try:
            resp = openai_client.chat.completions.create(
                model=model, messages=messages, max_tokens=4096, stream=True)
            for chunk in resp:
                delta = chunk.choices[0].delta.content
                if delta: yield delta
        except Exception as e:
            yield f"\n\n*Error: {e}*"

    else:
        yield f"\n\n*Error: Unknown plan '{plan}'*"


def ai_code(prompt, plan):
    """Code assistance using plan-appropriate model."""
    model = get_code_model(plan)
    system = CODE_SYSTEM

    if plan == "free":
        try:
            model_obj = genai.GenerativeModel(model)
            res = model_obj.generate_content(f"{system}\n\nUser: {prompt}")
            return res.text.strip()
        except Exception as e:
            return f"Error: Gemini Code failed: {e}"

    elif plan == "basic":
        # Claude Haiku
        try:
            msg = claude_client.messages.create(
                model=model, max_tokens=2048, system=system,
                messages=[{"role": "user", "content": prompt}])
            return msg.content[0].text
        except Exception as e:
            return f"Error: {e}"

    elif plan == "medium":
        # Claude Sonnet
        try:
            msg = claude_client.messages.create(
                model=model, max_tokens=4096, system=system,
                messages=[{"role": "user", "content": prompt}])
            return msg.content[0].text
        except Exception as e:
            return f"Error: {e}"

    elif plan == "elite":
        # Claude Opus — best in class
        try:
            msg = claude_client.messages.create(
                model=model, max_tokens=8096, system=system,
                messages=[{"role": "user", "content": prompt}])
            return msg.content[0].text
        except Exception as e:
            return f"Error: {e}"

    return f"Error: Unknown plan '{plan}' for code assistance."


def ai_generate_image(prompt, plan):
    """Image generation using plan-appropriate provider"""
    provider = get_image_provider(plan)

    if provider == "pollinations":
        clean = re.sub(r"[^\w\s-]", "", prompt)[:200].replace(" ", "%20")
        return f"https://image.pollinations.ai/prompt/{clean}?width=1024&height=768&nologo=true", None

    elif provider == "dalle3":
        try:
            resp = openai_client.images.generate(
                model="dall-e-3", prompt=prompt, n=1,
                size="1024x1024", quality="standard")
            return resp.data[0].url, resp.data[0].revised_prompt
        except Exception as e:
            return None, str(e)

    elif provider == "gemini":
        try:
            model = genai.GenerativeModel("gemini-2.0-flash-preview-image-generation")
            response = model.generate_content(
                f"Generate a high quality image: {prompt}",
                generation_config={"response_modalities": ["IMAGE", "TEXT"]})
            import base64
            img_dir = os.path.join(app.static_folder, "img")
            os.makedirs(img_dir, exist_ok=True)
            for part in response.candidates[0].content.parts:
                if part.inline_data:
                    img_bytes = part.inline_data.data
                    fname = f"img_{secrets.token_hex(8)}.png"
                    path = os.path.join(img_dir, fname)
                    with open(path, "wb") as f:
                        f.write(base64.b64decode(img_bytes) if isinstance(img_bytes, str) else img_bytes)
                    return f"/static/img/{fname}", None
            return None, "Gemini returned no image data."
        except Exception as e:
            print(f"[FALLBACK] Gemini Image failed: {e}")
            # Fallback to Pollinations for FREE users only (Basic/Elite should just error to retry)
            if plan == "free":
                clean = re.sub(r"[^\w\s-]", "", prompt)[:200].replace(" ", "%20")
                return f"https://image.pollinations.ai/prompt/{clean}?width=1024&height=768&nologo=true", None
            return None, str(e)


def ai_query(prompt, system, plan="free", large=False):
    """Non-streaming query for notes/tools."""
    if plan == "free":
        model = "gemini-1.5-flash"
        try:
            m = genai.GenerativeModel(model)
            res = m.generate_content(f"{system}\n\nUser: {prompt}")
            return res.text.strip()
        except Exception as e:
            return f"Error: Gemini Query failed: {e}"
    elif plan == "basic":
        try:
            res = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                max_tokens=3000)
            return res.choices[0].message.content.strip()
        except Exception as e:
            return f"Error: {e}"
    elif plan in ("medium", "elite"):
        # SILENT DOWNGRADE FOR PROFIT:
        # If the task is simple (grammar or translate), use gpt-4o-mini even for elite users.
        is_simple_task = (system == GRAMMAR_SYSTEM) or (system == TRANSLATE_SYSTEM)
        actual_model = "gpt-4o-mini" if is_simple_task else "gpt-4o"
        
        try:
            res = openai_client.chat.completions.create(
                model=actual_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                max_tokens=6000)
            return res.choices[0].message.content.strip()
        except Exception as e:
            return f"Error: {e}"


# ════════════════════════════════════════════
#  SYSTEM PROMPTS
# ════════════════════════════════════════════

CODE_SYSTEM = """You are NexusAI Code — an elite programming assistant.
Help with code: generate, debug, explain, review, optimize, convert.
Use markdown code blocks with proper language tags. Explain clearly.
For Elite users: provide production-ready, enterprise-grade code with tests, error handling, documentation."""

QA_SYSTEM = """You are an expert exam question generator and study notes creator.
Output ONLY a JSON object (no markdown, no extra text) with this exact structure:
{
  "title": "Topic title here",
  "summary": "2-3 sentence overview",
  "key_points": ["point 1", "point 2", ...],
  "questions": [
    {"q": "Question?", "a": "Detailed answer.", "type": "short"}
  ]
}
Generate 8-15 questions. Mix: short answer, long answer, true/false, fill-in-the-blank.
Output ONLY valid JSON."""

QUIZ_SYSTEM = """Generate a 10-question MCQ quiz. Output ONLY a JSON array:
[{"q":"Question?","options":["A) opt1","B) opt2","C) opt3","D) opt4"],"answer":"A","explanation":"Why A."}]
Output ONLY valid JSON array."""

FLASHCARD_SYSTEM = """Generate 15-20 flashcards. Output ONLY JSON array:
[{"front":"Term","back":"Definition"}]
Output ONLY valid JSON array."""

SUMMARIZE_SYSTEM = """Summarize text. Output ONLY JSON:
{"tldr":"One sentence","bullets":["key point"],"full_summary":"2-3 paragraph summary"}
Output ONLY valid JSON."""

GRAMMAR_SYSTEM = """Check grammar/spelling/style. Output ONLY JSON:
{"corrected":"Full corrected text","issues":["issue 1"],"score":85}
Output ONLY valid JSON."""

TRANSLATE_SYSTEM = "Professional translator. Respond with ONLY the translation."

EXPLAIN_SYSTEM = """Expert teacher. Explain clearly with examples and analogies.
Format with markdown headers, bullets. Be thorough."""


def generate_chat_title(message, plan="free"):
    try:
        if plan == "free":
            model = genai.GenerativeModel("gemini-1.5-flash")
            res = model.generate_content(f"Generate a short chat title in max 6 words for this message: {message[:300]}. Output ONLY the title, no quotes.")
            return res.text.strip()
        
        # For other plans
        if OPENAI_KEY:
            res = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Generate a short chat title in max 6 words. No quotes, no punctuation at end."},
                    {"role": "user", "content": message[:300]}],
                max_tokens=20)
            return res.choices[0].message.content.strip()
        return message[:40]
    except:
        return message[:40]


def generate_notes_from_text(source_text, extra="", plan="free"):
    prompt = f"Source material:\n---\n{source_text[:6000]}\n---\n{extra}\nGenerate comprehensive study notes and questions."
    raw = ai_query(prompt, QA_SYSTEM, plan=plan, large=True)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except:
        return {
            "title": "Study Notes", "summary": "AI-generated notes.",
            "key_points": ["Review carefully."],
            "questions": [{"q": "Summarise the main topic.", "a": raw[:500], "type": "long"}]
        }


def generate_quiz(text, plan="free"):
    raw = ai_query(f"Generate quiz from:\n\n{text[:4000]}", QUIZ_SYSTEM, plan=plan, large=True)
    raw = raw.strip()
    if raw.startswith("```"): raw = re.sub(r"^```[a-z]*\n?","",raw); raw = re.sub(r"```$","",raw).strip()
    try: return json.loads(raw)
    except: return []


def generate_flashcards(text, plan="free"):
    raw = ai_query(f"Generate flashcards from:\n\n{text[:4000]}", FLASHCARD_SYSTEM, plan=plan, large=True)
    raw = raw.strip()
    if raw.startswith("```"): raw = re.sub(r"^```[a-z]*\n?","",raw); raw = re.sub(r"```$","",raw).strip()
    try: return json.loads(raw)
    except: return []

# ════════════════════════════════════════════
#  PDF BUILDER
# ════════════════════════════════════════════

def build_notes_pdf(data):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm,
        title=data.get("title","Notes"), author="NexusAI")
    GOLD  = colors.HexColor("#f59e0b")
    DARK  = colors.HexColor("#0a0a0f")
    SLATE = colors.HexColor("#1a1a2e")
    LIGHT = colors.HexColor("#e2e8f0")
    MUTED = colors.HexColor("#94a3b8")

    ts = ParagraphStyle("T", fontName="Helvetica-Bold", fontSize=22, textColor=GOLD,
        spaceAfter=6, alignment=TA_CENTER, leading=28)
    ss = ParagraphStyle("S", fontName="Helvetica", fontSize=10, textColor=MUTED,
        spaceAfter=4, alignment=TA_CENTER)
    hs = ParagraphStyle("H", fontName="Helvetica-Bold", fontSize=13, textColor=GOLD,
        spaceBefore=14, spaceAfter=6, leading=16)
    bs = ParagraphStyle("B", fontName="Helvetica", fontSize=10, textColor=LIGHT,
        leading=15, spaceAfter=5, alignment=TA_JUSTIFY)
    bls= ParagraphStyle("BL", fontName="Helvetica", fontSize=10, textColor=LIGHT,
        leading=14, spaceAfter=3, leftIndent=14)
    qs = ParagraphStyle("Q", fontName="Helvetica-Bold", fontSize=10, textColor=LIGHT,
        leading=14, spaceBefore=8, spaceAfter=3)
    as_ = ParagraphStyle("A", fontName="Helvetica", fontSize=10,
        textColor=colors.HexColor("#94a3b8"), leading=14, leftIndent=12, spaceAfter=4)

    story = []
    hd = [[Paragraph(data.get("title","Notes"), ts)]]
    ht = Table(hd, colWidths=[17*cm])
    ht.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),DARK),
        ("ROWPADDING",(0,0),(-1,-1),14),("BOX",(0,0),(-1,-1),1,GOLD),("VALIGN",(0,0),(-1,-1),"MIDDLE")]))
    story.extend([ht, Spacer(1,0.3*cm),
        Paragraph("Generated by NexusAI · Premium Study Notes", ss),
        HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=10)])

    if data.get("summary"):
        story.extend([Paragraph("Overview", hs), Paragraph(data["summary"], bs)])
    if data.get("key_points"):
        story.append(Paragraph("Key Points", hs))
        for pt in data["key_points"]: story.append(Paragraph(f"• {pt}", bls))
    story.extend([Spacer(1,0.4*cm), HRFlowable(width="100%",thickness=0.5,color=SLATE,spaceAfter=8)])

    if data.get("questions"):
        story.append(Paragraph("Questions & Answers", hs))
        for i, item in enumerate(data["questions"], 1):
            story.append(Paragraph(f"<b>Q{i}.</b> {item.get('q','')}", qs))
            story.append(Paragraph(f"<font color='#94a3b8'>Answer: </font>{item.get('a','')}", as_))
            if i < len(data["questions"]):
                story.append(HRFlowable(width="100%",thickness=0.3,color=SLATE,spaceAfter=4,spaceBefore=4))

    story.extend([Spacer(1,0.6*cm),
        HRFlowable(width="100%",thickness=1,color=GOLD,spaceAfter=6),
        Paragraph("NexusAI Premium · Powered by GPT-4o / Claude Opus", ss)])
    doc.build(story); buf.seek(0)
    return buf.read()

# ════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════

def get_db():
    if not DATABASE_URL: raise Exception("DATABASE_URL missing")
    # FIX #10: Make SSL mode configurable — use 'require' for production, 'prefer' or 'disable' for local dev
    ssl_mode = os.getenv("DB_SSLMODE", "require")
    return psycopg2.connect(DATABASE_URL, sslmode=ssl_mode)

def init_db():
    conn = get_db(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        id SERIAL PRIMARY KEY, username TEXT UNIQUE, email TEXT UNIQUE,
        phone TEXT, password TEXT, bio TEXT DEFAULT '', avatar_url TEXT DEFAULT '',
        plan TEXT DEFAULT 'free', plan_expires_at TIMESTAMP,
        stripe_customer_id TEXT, stripe_subscription_id TEXT,
        email_verified BOOLEAN DEFAULT FALSE, phone_verified BOOLEAN DEFAULT FALSE,
        email_token TEXT, phone_otp TEXT, otp_expires_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS conversations(
        id SERIAL PRIMARY KEY, username TEXT, title TEXT,
        mode TEXT DEFAULT 'chat', pinned BOOLEAN DEFAULT FALSE,
        model_used TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS chats(
        id SERIAL PRIMARY KEY, conversation_id INTEGER, username TEXT,
        user_msg TEXT, bot_msg TEXT, bot_plain TEXT, type TEXT DEFAULT 'text',
        model_used TEXT DEFAULT '', tokens_used INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    try:
        c.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS model_used TEXT DEFAULT ''")
        c.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS model_used TEXT DEFAULT ''")
        c.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS tokens_used INTEGER DEFAULT 0")
        c.execute("ALTER TABLE notes ADD COLUMN IF NOT EXISTS quiz_json TEXT")
        c.execute("ALTER TABLE notes ADD COLUMN IF NOT EXISTS flashcards_json TEXT")
        c.execute("ALTER TABLE notes ADD COLUMN IF NOT EXISTS plan_used TEXT DEFAULT 'free'")
    except: pass
    c.execute("""CREATE TABLE IF NOT EXISTS notes(
        id SERIAL PRIMARY KEY, username TEXT NOT NULL, title TEXT,
        source_text TEXT, notes_json TEXT, quiz_json TEXT, flashcards_json TEXT,
        plan_used TEXT DEFAULT 'free',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS bookmarks(
        id SERIAL PRIMARY KEY, username TEXT NOT NULL, chat_id INTEGER,
        note TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS reminders(
        id SERIAL PRIMARY KEY, username TEXT NOT NULL, title TEXT,
        body TEXT, remind_at TIMESTAMP, done BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS usage_logs(
        id SERIAL PRIMARY KEY, username TEXT, action TEXT, plan TEXT,
        model TEXT, tokens_used INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS payments(
        id SERIAL PRIMARY KEY, username TEXT, stripe_session_id TEXT,
        plan TEXT, amount NUMERIC, status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS password_resets(
        id SERIAL PRIMARY KEY, username TEXT, token TEXT UNIQUE,
        expires_at TIMESTAMP, used BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS stories(
        id SERIAL PRIMARY KEY, username TEXT, content TEXT,
        media_url TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS feedback(
        id SERIAL PRIMARY KEY, username TEXT, rating INTEGER,
        message TEXT, page TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    # Migrations — safe to run repeatedly
    for sql in [
        "ALTER TABLE chats ADD COLUMN IF NOT EXISTS model_used TEXT DEFAULT ''",
        "ALTER TABLE chats ADD COLUMN IF NOT EXISTS tokens_used INTEGER DEFAULT 0",
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS model_used TEXT DEFAULT ''",
        "ALTER TABLE notes ADD COLUMN IF NOT EXISTS plan_used TEXT DEFAULT 'free'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'free'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_expires_at TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_token TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_otp TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS otp_expires_at TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT",
    ]:
        try:
            c.execute(sql)
        except Exception:
            pass
    conn.commit(); conn.close()

# ════════════════════════════════════════════
#  VERIFICATION HELPERS
# ════════════════════════════════════════════

def send_email_verification(email, token):
    """Send verification email via SendGrid"""
    # sg = SendGridAPIClient(os.getenv("SENDGRID_API_KEY"))
    # link = url_for('verify_email', token=token, _external=True)
    # message = Mail(from_email='noreply@nexusai.app', to_emails=email,
    #     subject='Verify your NexusAI email',
    #     html_content=f'<a href="{link}">Click to verify your email</a>')
    # sg.send(message)
    print(f"[EMAIL] Verification token for {email}: {token}")  # dev mode

def send_phone_otp(phone, otp):
    """Send OTP via Twilio"""
    # client = Client(os.getenv("TWILIO_SID"), os.getenv("TWILIO_TOKEN"))
    # client.messages.create(body=f'NexusAI OTP: {otp}', from_=os.getenv("TWILIO_FROM"), to=phone)
    print(f"[SMS] OTP for {phone}: {otp}")  # dev mode

# ════════════════════════════════════════════
#  AUTH ROUTES
# ════════════════════════════════════════════

@app.route("/login", methods=["GET","POST"])
def login():
    error = None
    if request.method == "POST":
        user = request.form.get("username","").strip()
        pwd  = request.form.get("password","")
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT password, plan FROM users WHERE username=%s OR email=%s", (user, user))
        row = c.fetchone()
        if row:
            stored = row[0]
            valid = bcrypt.checkpw(pwd.encode(), stored.encode()) if stored.startswith("$2b$") else pwd == stored
            if valid:
                # FIX #1: Removed broken c.execute() result assignment.
                # Always set a default first, then re-fetch the canonical username if login was by email.
                session["user"] = user
                if "@" in user:
                    c.execute("SELECT username FROM users WHERE email=%s", (user,))
                    r = c.fetchone()
                    if r: session["user"] = r[0]
                conn.close()
                return redirect("/")
        conn.close()
        error = "Invalid credentials."
    return render_template("login.html", error=error)


@app.route("/register", methods=["GET","POST"])
def register():
    error = None
    if request.method == "POST":
        username = request.form.get("username","").strip()
        email    = request.form.get("email","").strip()
        phone    = request.form.get("phone","").strip()
        pwd      = request.form.get("password","")
        if len(username) < 3: error = "Username must be ≥ 3 characters."
        elif len(pwd) < 6: error = "Password must be ≥ 6 characters."
        elif not email or "@" not in email: error = "Valid email required."
        else:
            try:
                hashed = bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()
                token  = secrets.token_urlsafe(32)
                otp    = str(secrets.randbelow(900000) + 100000)
                conn = get_db(); c = conn.cursor()
                c.execute("""INSERT INTO users (username, email, phone, password, email_token, phone_otp, otp_expires_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (username, email, phone or None, hashed, token, otp,
                     datetime.utcnow() + timedelta(minutes=15)))
                conn.commit(); conn.close()
                send_email_verification(email, token)
                if phone: send_phone_otp(phone, otp)
                session["pending_user"] = username
                return redirect("/verify")
            except psycopg2.errors.UniqueViolation:
                error = "Username or email already exists."
            except Exception as e:
                error = f"Registration error: {e}"
    return render_template("register.html", error=error)


@app.route("/verify", methods=["GET","POST"])
def verify():
    error = None
    username = session.get("pending_user") or session.get("user")
    if not username: return redirect("/login")
    if request.method == "POST":
        otp = request.form.get("otp","").strip()
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT phone_otp, otp_expires_at FROM users WHERE username=%s", (username,))
        row = c.fetchone()
        # FIX #5: Use hmac.compare_digest() for constant-time OTP comparison (prevents timing attacks)
        stored_otp = row[0] if row else ""
        otp_valid = bool(stored_otp) and hmac.compare_digest(stored_otp, otp)
        if row and otp_valid and row[1] > datetime.utcnow():
            c.execute("UPDATE users SET phone_verified=TRUE, phone_otp=NULL WHERE username=%s", (username,))
            conn.commit(); conn.close()
            session["user"] = username
            session.pop("pending_user", None)
            return redirect("/")
        conn.close()
        error = "Invalid or expired OTP."
    return render_template("verify.html", error=error, has_email=True)


@app.route("/verify/email/<token>")
def verify_email(token):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT username FROM users WHERE email_token=%s", (token,))
    row = c.fetchone()
    if row:
        c.execute("UPDATE users SET email_verified=TRUE, email_token=NULL WHERE email_token=%s", (token,))
        conn.commit(); conn.close()
        return render_template("verified.html", success=True)
    conn.close()
    return render_template("verified.html", success=False)


@app.route("/resend-otp", methods=["POST"])
def resend_otp():
    username = session.get("pending_user") or session.get("user")
    if not username: return jsonify({"error": "No session"}), 401
    otp = str(secrets.randbelow(900000) + 100000)
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT phone FROM users WHERE username=%s", (username,))
    row = c.fetchone()
    if row and row[0]:
        c.execute("UPDATE users SET phone_otp=%s, otp_expires_at=%s WHERE username=%s",
            (otp, datetime.utcnow() + timedelta(minutes=15), username))
        conn.commit(); conn.close()
        send_phone_otp(row[0], otp)
        return jsonify({"ok": True})
    conn.close()
    return jsonify({"error": "No phone on file"})


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ════════════════════════════════════════════
#  STRIPE PAYMENT ROUTES
# ════════════════════════════════════════════

@app.route("/pricing")
def pricing():
    if "user" not in session: return redirect("/login")
    plan = get_user_plan(session["user"])
    return render_template("pricing.html", plans=PLANS, current_plan=plan, username=session["user"])


@app.route("/checkout/<plan_id>", methods=["POST"])
def checkout(plan_id):
    if "user" not in session: return redirect("/login")
    if plan_id not in ("basic", "medium", "elite"): return redirect("/pricing")
    price_map = {
        "basic":  STRIPE_BASIC_PRICE_ID,
        "medium": STRIPE_MEDIUM_PRICE_ID,
        "elite":  STRIPE_ELITE_PRICE_ID,
    }
    price_id = price_map[plan_id]
    try:
        # Get or create Stripe customer
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT email, stripe_customer_id FROM users WHERE username=%s", (session["user"],))
        row = c.fetchone(); conn.close()
        email, cust_id = row[0], row[1]

        if not cust_id:
            customer = stripe.Customer.create(email=email, metadata={"username": session["user"]})
            cust_id = customer.id
            conn2 = get_db(); c2 = conn2.cursor()
            c2.execute("UPDATE users SET stripe_customer_id=%s WHERE username=%s", (cust_id, session["user"]))
            conn2.commit(); conn2.close()

        sess = stripe.checkout.Session.create(
            customer=cust_id,
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=url_for("payment_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("pricing", _external=True),
            metadata={"username": session["user"], "plan": plan_id})

        # Log pending payment
        conn3 = get_db(); c3 = conn3.cursor()
        c3.execute("INSERT INTO payments (username, stripe_session_id, plan, amount, status) VALUES (%s,%s,%s,%s,'pending')",
            (session["user"], sess.id, plan_id, PLANS[plan_id]["price"]))
        conn3.commit(); conn3.close()
        return redirect(sess.url, code=303)
    except Exception as e:
        return f"Payment error: {e}", 500


@app.route("/payment/success")
def payment_success():
    if "user" not in session: return redirect("/login")
    sess_id = request.args.get("session_id")
    if sess_id:
        try:
            sess = stripe.checkout.Session.retrieve(sess_id)
            plan_id = sess.metadata.get("plan","basic")
            sub_id  = sess.subscription
            expires = datetime.utcnow() + timedelta(days=31)
            conn = get_db(); c = conn.cursor()
            c.execute("UPDATE users SET plan=%s, plan_expires_at=%s, stripe_subscription_id=%s WHERE username=%s",
                (plan_id, expires, sub_id, session["user"]))
            c.execute("UPDATE payments SET status='paid' WHERE stripe_session_id=%s", (sess_id,))
            conn.commit(); conn.close()
            if redis_client:
                try: redis_client.delete(f"plan:{session['user']}")
                except: pass
        except: pass
    return render_template("payment_success.html", username=session["user"])


@app.route("/payment/cancel")
def payment_cancel():
    return redirect("/pricing")


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except: return "Bad signature", 400

    if event["type"] == "customer.subscription.deleted":
        cust_id = event["data"]["object"]["customer"]
        conn = get_db(); c = conn.cursor()
        c.execute("UPDATE users SET plan='free', plan_expires_at=NULL WHERE stripe_customer_id=%s RETURNING username", (cust_id,))
        row = c.fetchone()
        conn.commit(); conn.close()
        if row and redis_client:
            try: redis_client.delete(f"plan:{row[0]}")
            except: pass
    elif event["type"] == "invoice.payment_succeeded":
        sub = event["data"]["object"].get("subscription")
        if sub:
            conn = get_db(); c = conn.cursor()
            c.execute("UPDATE users SET plan_expires_at=%s WHERE stripe_subscription_id=%s RETURNING username",
                (datetime.utcnow() + timedelta(days=31), sub))
            row = c.fetchone()
            conn.commit(); conn.close()
            if row and redis_client:
                try: redis_client.delete(f"plan:{row[0]}")
                except: pass
    return "OK", 200

# ════════════════════════════════════════════
#  MAIN DASHBOARD
# ════════════════════════════════════════════

@app.route("/")
def home():
    if "user" not in session: return redirect("/login")
    plan = get_user_plan(session["user"])
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT id,title,mode,pinned FROM conversations WHERE username=%s ORDER BY pinned DESC,id DESC", (session["user"],))
        convs = c.fetchall()
        c.execute("SELECT id,title,created_at FROM notes WHERE username=%s ORDER BY id DESC LIMIT 20", (session["user"],))
        notes_list = c.fetchall()
        c.execute("SELECT id,title,remind_at FROM reminders WHERE username=%s AND done=FALSE AND remind_at>NOW() ORDER BY remind_at LIMIT 5", (session["user"],))
        reminders = c.fetchall()
        c.execute("SELECT email_verified, phone_verified FROM users WHERE username=%s", (session["user"],))
        vrow = c.fetchone()
        conn.close()
        email_v = vrow[0] if vrow else False
        phone_v = vrow[1] if vrow else False
    except Exception as e:
        print("DASHBOARD ERROR:", e)
        convs=[]; notes_list=[]; reminders=[]; email_v=False; phone_v=False
    return render_template("dashboard.html", chat=[], conversations=convs,
        active_chat=None, notes_list=notes_list, reminders=reminders,
        username=session["user"], plan=plan, plans=PLANS,
        email_verified=email_v, phone_verified=phone_v)


@app.route("/new_chat")
def new_chat():
    session.pop("conv_id", None)
    return redirect("/")


@app.route("/chat/<int:conv_id>")
def open_chat(conv_id):
    if "user" not in session: return redirect("/login")
    plan = get_user_plan(session["user"])
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id FROM conversations WHERE id=%s AND username=%s", (conv_id, session["user"]))
    if not c.fetchone(): conn.close(); return redirect("/")
    session["conv_id"] = conv_id
    c.execute("SELECT user_msg,bot_msg,type,model_used FROM chats WHERE conversation_id=%s ORDER BY id", (conv_id,))
    rows = c.fetchall()
    c.execute("SELECT id,title,mode,pinned FROM conversations WHERE username=%s ORDER BY pinned DESC,id DESC", (session["user"],))
    convs = c.fetchall()
    c.execute("SELECT id,title,created_at FROM notes WHERE username=%s ORDER BY id DESC LIMIT 20", (session["user"],))
    notes_list = c.fetchall()
    c.execute("SELECT id,title,remind_at FROM reminders WHERE username=%s AND done=FALSE AND remind_at>NOW() ORDER BY remind_at LIMIT 5", (session["user"],))
    reminders = c.fetchall()
    conn.close()
    chat = [{"type":r[2],"user":r[0],"bot":r[1] if r[2]=="text" else None,
             "image":r[1] if r[2]=="image" else None,"model":r[3]} for r in rows]
    return render_template("dashboard.html", chat=chat, conversations=convs,
        active_chat=conv_id, notes_list=notes_list, reminders=reminders,
        username=session["user"], plan=plan, plans=PLANS,
        email_verified=True, phone_verified=True)

# ════════════════════════════════════════════
#  CHAT ROUTES
# ════════════════════════════════════════════

@app.route("/tool", methods=["POST"])
def tool():
    if "user" not in session: return redirect("/login")
    plan = get_user_plan(session["user"])

    # Credit check
    used, limit, ok = check_credits(session["user"], "chat")
    if not ok:
        return jsonify({"error": f"Daily credit limit reached ({limit} credits). Upgrade for more."}), 429

    user_input = request.form.get("input","").strip()
    mode = request.form.get("mode","chat")

    lower = user_input.lower()
    if any(lower.startswith(p) for p in ["generate image","create image","draw:","image:","/image ","draw "]):
        # Image generation
        if not plan_has_feature(plan, "basic_chat"):
            return redirect("/pricing")
        prompt = re.sub(r'^(generate image[: ]|create image[: ]|draw[: ]|image[: ]|/image )', '', user_input, flags=re.IGNORECASE).strip()
        img_url, revised = ai_generate_image(prompt, plan)
        if not img_url: img_url = f"https://image.pollinations.ai/prompt/{prompt.replace(' ','%20')}?width=1024&height=768"

        conn = get_db(); c = conn.cursor()
        conv_id = session.get("conv_id")
        if not conv_id:
            title = generate_chat_title(user_input, plan)
            c.execute("INSERT INTO conversations (username,title,model_used) VALUES (%s,%s,%s) RETURNING id",
                (session["user"], title, get_image_provider(plan)))
            conv_id = c.fetchone()[0]; session["conv_id"] = conv_id
        c.execute("INSERT INTO chats (conversation_id,username,user_msg,bot_msg,type,model_used) VALUES (%s,%s,%s,%s,'image',%s)",
            (conv_id, session["user"], user_input, img_url, get_image_provider(plan)))
        conn.commit(); conn.close()
        return redirect(f"/chat/{conv_id}")

    conn = get_db(); c = conn.cursor()
    conv_id = session.get("conv_id")
    if not conv_id:
        title = generate_chat_title(user_input or "New Chat", plan)
        c.execute("INSERT INTO conversations (username,title,mode,model_used) VALUES (%s,%s,%s,%s) RETURNING id",
            (session["user"], title, mode, get_chat_model(plan)))
        conv_id = c.fetchone()[0]; session["conv_id"] = conv_id
        conn.commit()
    session["pending_prompt"] = user_input
    session["pending_mode"] = mode
    conn.close()
    return redirect(f"/chat/{conv_id}?do_stream=true")


@app.route("/api/chat/stream", methods=["POST"])
def api_chat_stream_route():
    if "user" not in session: return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    prompt = data.get("prompt")
    mode = data.get("mode", "chat")
    conv_id = data.get("conv_id")
    username = session["user"]
    plan = get_user_plan(username)
    
    # Credit check
    _, limit, ok = check_credits(username, "chat")
    if not ok:
        return jsonify({"error": f"Daily credit limit reached ({limit} credits)."}), 429

    conn = get_db(); c = conn.cursor()
    if not conv_id:
        title = generate_chat_title(prompt or "New Chat", plan)
        c.execute("INSERT INTO conversations (username,title,mode,model_used) VALUES (%s,%s,%s,%s) RETURNING id",
            (username, title, mode, get_chat_model(plan)))
        conv_id = c.fetchone()[0]
        conn.commit()
    else:
        # Verify ownership
        c.execute("SELECT id FROM conversations WHERE id=%s AND username=%s", (conv_id, username))
        if not c.fetchone():
            conn.close()
            return jsonify({"error": "Conversation not found"}), 404
    conn.close()

    system_map = {
        "code": CODE_SYSTEM,
        "explain": EXPLAIN_SYSTEM,
        "default": f"You are NexusAI, an elite AI assistant powered by {'GPT-4o' if plan in ('medium','elite') else 'GPT-4o-mini' if plan=='basic' else 'Llama 3.1'}. Answer clearly and thoroughly. Plan: {plan.upper()}."
    }
    system = system_map.get(mode, system_map["default"])
    model_used = get_chat_model(plan)

    def generate():
        history = []
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("SELECT user_msg,bot_plain FROM chats WHERE conversation_id=%s ORDER BY id DESC LIMIT 10", (conv_id,))
            rows = c.fetchall(); conn.close()
            history = [{"user": r[0], "bot_plain": r[1]} for r in reversed(rows)]
        except: pass

        full_content = []
        token_count = 0
        
        try:
            for chunk in ai_chat_stream(prompt, system, history, plan):
                full_content.append(chunk)
                # Rough token estimation for live display
                token_count += len(chunk.split()) if chunk.strip() else 0
                yield f"data: {json.dumps({'chunk': chunk, 'tokens': token_count})}\n\n"
            
            complete = "".join(full_content)
            formatted = markdown.markdown(complete)
            conn = get_db(); c = conn.cursor()
            c.execute("""INSERT INTO chats (conversation_id,username,user_msg,bot_msg,bot_plain,type,model_used)
                VALUES (%s,%s,%s,%s,%s,'text',%s)""",
                (conv_id, username, prompt, formatted, complete, model_used))
            conn.commit(); conn.close()
            spend_credits(username, plan, "chat", model_used)
            
            yield f"data: {json.dumps({'done': True, 'conv_id': conv_id, 'tokens': token_count})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

@app.route("/api/generate_video", methods=["POST"])
def api_generate_video():
    if "user" not in session: return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    if not prompt: return jsonify({"error": "Prompt required"}), 400
    
    plan = get_user_plan(session["user"])
    if not plan_has_feature(plan, "video_gen"):
        return jsonify({"error": "Video generation requires Basic plan or higher."}), 403
    
    used, limit, ok = check_credits(session["user"], "video")
    if not ok:
        cost = PLAN_CREDIT_COSTS[plan].get("video", 1000)
        return jsonify({"error": f"Insufficient credits. Video generation costs {cost} credits."}), 403
    
    # Simulate video generation
    # Use a high-quality placeholder for demonstration
    time.sleep(2) 
    video_url = "https://cdn.pixabay.com/vimeo/327334710/aurora-23136.mp4?width=1280&hash=8b5b7b9b1b2b3b4b5b6b7b8b9b0b1b2b3b4b5b6"
    
    spend_credits(session["user"], plan, "video", model="Nexus-Video-Gen")
    
    return jsonify({
        "ok": True,
        "video_url": video_url,
        "prompt": prompt,
        "credits_spent": PLAN_CREDIT_COSTS[plan].get("video", 1000)
    })


@app.route("/stream")
def stream():
    if "user" not in session: return Response("Unauthorized", status=401)
    prompt   = session.pop("pending_prompt","Hello")
    mode     = session.pop("pending_mode","chat")
    conv_id  = session.get("conv_id")
    username = session["user"]
    plan     = get_user_plan(username)

    system_map = {
        "code":    CODE_SYSTEM,
        "explain": EXPLAIN_SYSTEM,
        "default": f"You are NexusAI, an elite AI assistant powered by {'GPT-4o' if plan in ('medium','elite') else 'GPT-4o-mini' if plan=='basic' else 'Llama 3.1'}. Answer clearly and thoroughly. Plan: {plan.upper()}."
    }
    system = system_map.get(mode, system_map["default"])

    def generate():
        if not conv_id:
            yield f"data: {json.dumps({'error': 'Conversation error'})}\n\n"; return
        history = []
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("SELECT user_msg,bot_plain FROM chats WHERE conversation_id=%s ORDER BY id DESC LIMIT 10", (conv_id,))
            rows = c.fetchall(); conn.close()
            history = [{"user": r[0], "bot_plain": r[1]} for r in reversed(rows)]
        except: pass

        full = []
        model_used = get_chat_model(plan)
        try:
            # Code mode: use code AI for basic/medium/elite
            if mode == "code" and plan in ("basic", "medium", "elite"):
                result = ai_code(prompt, plan)
                full = [result]
                yield f"data: {json.dumps({'chunk': result})}\n\n"
            else:
                for chunk in ai_chat_stream(prompt, system, history, plan):
                    full.append(chunk)
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            complete = "".join(full)
            try: formatted = markdown.markdown(complete)
            except: formatted = complete

            try:
                conn = get_db(); c = conn.cursor()
                c.execute("""INSERT INTO chats (conversation_id,username,user_msg,bot_msg,bot_plain,type,model_used)
                    VALUES (%s,%s,%s,%s,%s,'text',%s)""",
                    (conv_id, username, prompt, formatted, complete, model_used))
                conn.commit(); conn.close()
            except Exception as e:
                print("DB ERROR:", e)
            spend_credits(username, plan, "code" if mode == "code" else "chat", model_used)
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

# ════════════════════════════════════════════
#  AI TOOL APIs
# ════════════════════════════════════════════

@app.route("/api/summarize", methods=["POST"])
def api_summarize():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    plan = get_user_plan(session["user"])
    _, limit, ok = check_credits(session["user"], "summarize")
    if not ok: return jsonify({"error": f"Not enough credits (need {PLAN_CREDIT_COSTS.get(plan, {}).get('summarize')}, limit {limit}/day)."}), 429
    body = request.get_json(force=True)
    text = body.get("text","").strip()
    if not text: return jsonify({"error":"No text"}),400
    raw = ai_query(f"Summarize:\n\n{text[:5000]}", SUMMARIZE_SYSTEM, plan=plan, large=True)
    raw = raw.strip()
    if raw.startswith("```"): raw = re.sub(r"^```[a-z]*\n?","",raw); raw=re.sub(r"```$","",raw).strip()
    spend_credits(session["user"], plan, "summarize")
    try: return jsonify({"ok":True,"data":json.loads(raw)})
    except: return jsonify({"ok":True,"data":{"tldr":raw[:200],"bullets":[],"full_summary":raw}})


@app.route("/api/grammar", methods=["POST"])
def api_grammar():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    plan = get_user_plan(session["user"])
    _, limit, ok = check_credits(session["user"], "grammar")
    if not ok: return jsonify({"error": f"Not enough credits (need {PLAN_CREDIT_COSTS.get(plan, {}).get('grammar')}, limit {limit}/day)."}), 429
    body = request.get_json(force=True)
    text = body.get("text","").strip()
    if not text: return jsonify({"error":"No text"}),400
    raw = ai_query(f"Check and correct:\n\n{text[:3000]}", GRAMMAR_SYSTEM, plan=plan)
    raw = raw.strip()
    if raw.startswith("```"): raw = re.sub(r"^```[a-z]*\n?","",raw); raw=re.sub(r"```$","",raw).strip()
    spend_credits(session["user"], plan, "grammar")
    try: return jsonify({"ok":True,"data":json.loads(raw)})
    except: return jsonify({"ok":True,"data":{"corrected":raw,"issues":[],"score":0}})


@app.route("/api/translate", methods=["POST"])
def api_translate():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    plan = get_user_plan(session["user"])
    _, limit, ok = check_credits(session["user"], "translate")
    if not ok: return jsonify({"error": f"Not enough credits (need {PLAN_CREDIT_COSTS.get(plan, {}).get('translate')}, limit {limit}/day)."}), 429
    body = request.get_json(force=True)
    text = body.get("text","").strip()
    lang = body.get("language","Spanish")
    if not text: return jsonify({"error":"No text"}),400
    result = ai_query(f"Translate to {lang}:\n\n{text[:3000]}", TRANSLATE_SYSTEM, plan=plan)
    spend_credits(session["user"], plan, "translate")
    return jsonify({"ok":True,"translation":result})


@app.route("/api/explain", methods=["POST"])
def api_explain():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    plan = get_user_plan(session["user"])
    _, limit, ok = check_credits(session["user"], "explain")
    if not ok: return jsonify({"error": f"Not enough credits (need {PLAN_CREDIT_COSTS.get(plan, {}).get('explain')}, limit {limit}/day)."}), 429
    body = request.get_json(force=True)
    topic = body.get("topic","").strip()
    level = body.get("level","intermediate")
    if not topic: return jsonify({"error":"No topic"}),400
    result = ai_query(f"Explain '{topic}' at a {level} level with examples and analogies.", EXPLAIN_SYSTEM, plan=plan, large=True)
    spend_credits(session["user"], plan, "explain")
    return jsonify({"ok":True,"explanation":markdown.markdown(result)})


@app.route("/api/code", methods=["POST"])
def api_code():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    plan = get_user_plan(session["user"])
    _, limit, ok = check_credits(session["user"], "code")
    if not ok: return jsonify({"error": f"Not enough credits (need {PLAN_CREDIT_COSTS.get(plan, {}).get('code')}, limit {limit}/day)."}), 429
    body = request.get_json(force=True)
    prompt = body.get("prompt","").strip()
    if not prompt: return jsonify({"error":"No prompt"}),400
    result = ai_code(prompt, plan)
    spend_credits(session["user"], plan, "code", get_code_model(plan))
    return jsonify({"ok":True,"result":markdown.markdown(result),"model":get_code_model(plan)})


@app.route("/api/quiz", methods=["POST"])
def api_quiz():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    plan = get_user_plan(session["user"])
    _, limit, ok = check_credits(session["user"], "quiz")
    if not ok: return jsonify({"error": f"Not enough credits (need {PLAN_CREDIT_COSTS.get(plan, {}).get('quiz')}, limit {limit}/day)."}), 429
    body = request.get_json(force=True)
    text = body.get("text","").strip()
    note_id = body.get("note_id")
    if note_id:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT source_text,quiz_json FROM notes WHERE id=%s AND username=%s", (note_id, session["user"]))
        row = c.fetchone(); conn.close()
        if row:
            if row[1]: return jsonify({"ok":True,"questions":json.loads(row[1])})
            text = row[0] or ""
    if not text: return jsonify({"error":"No content"}),400
    questions = generate_quiz(text, plan)
    if note_id and questions:
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("UPDATE notes SET quiz_json=%s WHERE id=%s", (json.dumps(questions), note_id))
            conn.commit(); conn.close()
        except: pass
    spend_credits(session["user"], plan, "quiz")
    return jsonify({"ok":True,"questions":questions})


@app.route("/api/flashcards", methods=["POST"])
def api_flashcards():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    plan = get_user_plan(session["user"])
    _, limit, ok = check_credits(session["user"], "flashcards")
    if not ok: return jsonify({"error": f"Not enough credits (need {PLAN_CREDIT_COSTS.get(plan, {}).get('flashcards')}, limit {limit}/day)."}), 429
    body = request.get_json(force=True)
    text = body.get("text","").strip()
    note_id = body.get("note_id")
    if note_id:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT source_text,flashcards_json FROM notes WHERE id=%s AND username=%s", (note_id, session["user"]))
        row = c.fetchone(); conn.close()
        if row:
            if row[1]: return jsonify({"ok":True,"cards":json.loads(row[1])})
            text = row[0] or ""
    if not text: return jsonify({"error":"No content"}),400
    cards = generate_flashcards(text, plan)
    if note_id and cards:
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("UPDATE notes SET flashcards_json=%s WHERE id=%s", (json.dumps(cards), note_id))
            conn.commit(); conn.close()
        except: pass
    spend_credits(session["user"], plan, "flashcards")
    return jsonify({"ok":True,"cards":cards})


# Elite-only: Gemini video analysis
@app.route("/api/video-analyze", methods=["POST"])
def api_video_analyze():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    plan = get_user_plan(session["user"])
    if plan != "elite": return jsonify({"error":"Elite plan required","upgrade":True}),403
    _, limit, ok = check_credits(session["user"], "video")
    if not ok: return jsonify({"error": f"Not enough credits (need {PLAN_CREDIT_COSTS.get(plan, {}).get('video')}, limit {limit}/day)."}), 429
    body = request.get_json(force=True)
    prompt = body.get("prompt","Describe this video in detail.")
    video_url = body.get("video_url","")
    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content([prompt, {"file_data": {"file_uri": video_url}}])
        spend_credits(session["user"], plan, "video")
        return jsonify({"ok":True,"result":response.text})
    except Exception as e:
        return jsonify({"error":str(e)}),500


# Basic plan and above: advanced image generation
@app.route("/api/advanced-image", methods=["POST"])
def api_advanced_image():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    plan = get_user_plan(session["user"])
    if plan not in ("basic", "medium", "elite"): return jsonify({"error":"Basic plan or above required","upgrade":True}),403
    _, limit, ok = check_credits(session["user"], "image")
    if not ok: return jsonify({"error": f"Not enough credits (need {PLAN_CREDIT_COSTS.get(plan, {}).get('image')}, limit {limit}/day)."}), 429
    body = request.get_json(force=True)
    prompt = body.get("prompt","")
    style  = body.get("style","photorealistic")
    full_prompt = f"{prompt}, style: {style}, high quality, detailed"
    img_url, revised = ai_generate_image(full_prompt, plan)
    spend_credits(session["user"], plan, "image", get_image_provider(plan))
    return jsonify({"ok":True,"url":img_url,"revised":revised})

# ════════════════════════════════════════════
#  NOTES ROUTES
# ════════════════════════════════════════════

@app.route("/generate_notes", methods=["POST"])
def generate_notes():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    plan = get_user_plan(session["user"])
    _, limit, ok = check_credits(session["user"], "notes")
    if not ok: return jsonify({"error":f"Not enough credits (need {PLAN_CREDIT_COSTS.get(plan, {}).get('notes')}, limit {limit}/day). Upgrade for more."}),429

    file  = request.files.get("file")
    text  = request.form.get("text","").strip()
    extra = request.form.get("instruction","").strip()
    source_text = text

    if file and file.filename:
        fname = file.filename.lower()
        if fname.endswith(".pdf"):
            with pdfplumber.open(file) as pdf:
                for page in pdf.pages: source_text += (page.extract_text() or "")
        elif fname.endswith((".txt",".md")):
            source_text += file.read().decode("utf-8", errors="ignore")
        elif OCR_AVAILABLE and any(fname.endswith(x) for x in [".png",".jpg",".jpeg",".webp"]):
            img = Image.open(file); source_text += pytesseract.image_to_string(img)

    if not source_text: return jsonify({"error":"No content provided."}),400
    notes_data = generate_notes_from_text(source_text, extra, plan)

    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO notes (username,title,source_text,notes_json,plan_used) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (session["user"], notes_data.get("title","Notes"), source_text[:4000], json.dumps(notes_data), plan))
    note_id = c.fetchone()[0]; conn.commit(); conn.close()
    spend_credits(session["user"], plan, "notes", get_chat_model(plan))
    return jsonify({"ok":True,"note_id":note_id,"data":notes_data,"model":get_chat_model(plan)})


@app.route("/notes")
def notes_page():
    if "user" not in session: return redirect("/login")
    plan = get_user_plan(session["user"])
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id,title,created_at FROM notes WHERE username=%s ORDER BY id DESC", (session["user"],))
    notes_list = c.fetchall()
    c.execute("SELECT id,title,mode,pinned FROM conversations WHERE username=%s ORDER BY pinned DESC,id DESC", (session["user"],))
    convs = c.fetchall(); conn.close()
    # FIX #7: Pass username to template (was missing, causing UndefinedError in templates)
    return render_template("notes.html", notes_list=notes_list, conversations=convs,
                           plan=plan, plans=PLANS, username=session["user"])


@app.route("/notes/<int:note_id>")
def view_note(note_id):
    if "user" not in session: return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT title,notes_json,created_at,source_text FROM notes WHERE id=%s AND username=%s", (note_id, session["user"]))
    row = c.fetchone(); conn.close()
    if not row: return redirect("/notes")
    data = json.loads(row[1])
    return render_template("note_view.html", note=data, note_id=note_id,
        created_at=row[2], has_source=bool(row[3]), plan=get_user_plan(session["user"]))


@app.route("/notes/<int:note_id>/pdf")
def download_note_pdf(note_id):
    if "user" not in session: return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT title,notes_json FROM notes WHERE id=%s AND username=%s", (note_id, session["user"]))
    row = c.fetchone(); conn.close()
    if not row: return "Not found",404
    data = json.loads(row[1]); pdf_bytes = build_notes_pdf(data)
    safe = re.sub(r"[^\w\s-]","",data.get("title","notes"))[:50].replace(" ","_")
    return Response(pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="NexusAI_{safe}.pdf"'})


@app.route("/notes/<int:note_id>/delete", methods=["POST"])
def delete_note(note_id):
    if "user" not in session: return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM notes WHERE id=%s AND username=%s", (note_id, session["user"]))
    conn.commit(); conn.close()
    return redirect("/notes")

# ════════════════════════════════════════════
#  BOOKMARKS / REMINDERS / PROFILE
# ════════════════════════════════════════════

@app.route("/api/bookmark", methods=["POST"])
def add_bookmark():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    body = request.get_json(force=True)
    chat_id = body.get("chat_id"); note = body.get("note","")
    if not chat_id: return jsonify({"error":"No chat_id"}),400
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO bookmarks (username,chat_id,note) VALUES (%s,%s,%s)", (session["user"],chat_id,note))
    conn.commit(); conn.close()
    return jsonify({"ok":True})


@app.route("/api/reminders", methods=["GET"])
def get_reminders():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id,title,body,remind_at,done FROM reminders WHERE username=%s ORDER BY remind_at", (session["user"],))
    rows = c.fetchall(); conn.close()
    return jsonify({"reminders":[{"id":r[0],"title":r[1],"body":r[2],"remind_at":str(r[3]),"done":r[4]} for r in rows]})


@app.route("/api/reminders", methods=["POST"])
def add_reminder():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    body = request.get_json(force=True)
    title = body.get("title","").strip(); remind_at = body.get("remind_at",""); rb = body.get("body","")
    if not title or not remind_at: return jsonify({"error":"Title and time required"}),400
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO reminders (username,title,body,remind_at) VALUES (%s,%s,%s,%s) RETURNING id",
        (session["user"],title,rb,remind_at))
    rid = c.fetchone()[0]; conn.commit(); conn.close()
    return jsonify({"ok":True,"id":rid})


@app.route("/api/reminders/<int:rid>/done", methods=["POST"])
def done_reminder(rid):
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE reminders SET done=TRUE WHERE id=%s AND username=%s", (rid, session["user"]))
    conn.commit(); conn.close()
    return jsonify({"ok":True})


@app.route("/profile")
def profile():
    if "user" not in session: return redirect("/login")
    plan = get_user_plan(session["user"])
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT username,email,phone,bio,avatar_url,created_at,email_verified,phone_verified,plan,plan_expires_at FROM users WHERE username=%s", (session["user"],))
    user = c.fetchone()
    c.execute("SELECT COUNT(*) FROM conversations WHERE username=%s", (session["user"],))
    chat_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM notes WHERE username=%s", (session["user"],))
    note_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM chats WHERE username=%s", (session["user"],))
    msg_count = c.fetchone()[0]
    c.execute("SELECT id,title,mode,pinned FROM conversations WHERE username=%s ORDER BY pinned DESC,id DESC", (session["user"],))
    convs = c.fetchall()
    c.execute("SELECT id,title,created_at FROM notes WHERE username=%s ORDER BY id DESC LIMIT 20", (session["user"],))
    notes_list = c.fetchall(); conn.close()
    return render_template("profile.html", user=user, chat_count=chat_count,
        note_count=note_count, msg_count=msg_count, conversations=convs,
        notes_list=notes_list, plan=plan, plans=PLANS)


@app.route("/profile/update", methods=["POST"])
def update_profile():
    if "user" not in session: return redirect("/login")
    bio = request.form.get("bio","")[:300]
    avatar_url = request.form.get("avatar_url","")[:500]
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE users SET bio=%s, avatar_url=%s WHERE username=%s", (bio, avatar_url, session["user"]))
    conn.commit(); conn.close()
    return redirect("/profile")


@app.route("/profile/change_password", methods=["POST"])
def change_password():
    if "user" not in session: return redirect("/login")
    old = request.form.get("old_password","")
    new = request.form.get("new_password","")
    if len(new) < 6: return redirect("/profile?error=Password+too+short")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT password FROM users WHERE username=%s", (session["user"],))
    row = c.fetchone()
    if not row: conn.close(); return redirect("/profile")
    stored = row[0]
    valid = bcrypt.checkpw(old.encode(), stored.encode()) if stored.startswith("$2b$") else old == stored
    if not valid: conn.close(); return redirect("/profile?error=Wrong+current+password")
    hashed = bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode()
    c.execute("UPDATE users SET password=%s WHERE username=%s", (hashed, session["user"]))
    conn.commit(); conn.close()
    return redirect("/profile?success=Password+changed")



@app.route("/delete_chat/<int:conv_id>", methods=["POST"])
def delete_chat(conv_id):
    if "user" not in session: return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM chats WHERE conversation_id=%s AND username=%s", (conv_id, session["user"]))
    c.execute("DELETE FROM conversations WHERE id=%s AND username=%s", (conv_id, session["user"]))
    conn.commit(); conn.close()
    if session.get("conv_id") == conv_id: session.pop("conv_id", None)
    return redirect("/")


@app.route("/edit_chat/<int:conv_id>", methods=["POST"])
def edit_chat(conv_id):
    if "user" not in session: return redirect("/login")
    title = request.form.get("title","").strip()
    if title:
        conn = get_db(); c = conn.cursor()
        c.execute("UPDATE conversations SET title=%s WHERE id=%s AND username=%s", (title,conv_id,session["user"]))
        conn.commit(); conn.close()
    return redirect("/")


@app.route("/pin_chat/<int:conv_id>", methods=["POST"])
def pin_chat(conv_id):
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT pinned FROM conversations WHERE id=%s AND username=%s", (conv_id, session["user"]))
    row = c.fetchone()
    if row:
        c.execute("UPDATE conversations SET pinned=%s WHERE id=%s AND username=%s", (not row[0],conv_id,session["user"]))
        conn.commit()
    conn.close()
    return jsonify({"ok":True})


@app.route("/export_chat/<int:conv_id>")
def export_chat(conv_id):
    if "user" not in session: return redirect("/login")
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT title FROM conversations WHERE id=%s AND username=%s", (conv_id, session["user"]))
    conv = c.fetchone()
    if not conv: conn.close(); return redirect("/")
    c.execute("SELECT user_msg,bot_plain,created_at,model_used FROM chats WHERE conversation_id=%s ORDER BY id", (conv_id,))
    rows = c.fetchall(); conn.close()
    lines = [f"NexusAI Chat Export: {conv[0]}", "="*60, ""]
    for r in rows:
        lines.append(f"You: {r[0]}")
        lines.append(f"NexusAI [{r[3]}]: {r[1] or '(image)'}")
        lines.append(f"[{r[2]}]"); lines.append("")
    content = "\n".join(lines)
    safe = re.sub(r"[^\w\s-]","",conv[0])[:40].replace(" ","_")
    return Response(content, mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="NexusAI_{safe}.txt"'})


@app.route("/quick_pdf", methods=["POST"])
def quick_pdf():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    body = request.get_json(force=True)
    notes_data = body.get("notes_data",{})
    if not notes_data: return jsonify({"error":"No data"}),400
    pdf_bytes = build_notes_pdf(notes_data)
    safe = re.sub(r"[^\w\s-]","",notes_data.get("title","notes"))[:40].replace(" ","_")
    return Response(pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="NexusAI_{safe}.pdf"'})


@app.route("/api/usage")
def api_usage():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    plan = get_user_plan(session["user"])
    used, limit, ok = check_credits(session["user"], "chat")
    return jsonify({
        "credits_used": used,
        "credits_limit": limit,
        "ok": ok,
        "plan": plan,
        "credit_costs": PLAN_CREDIT_COSTS.get(plan, {}),
        "credits_remaining": max(0, limit - used)
    })


@app.route("/api/plan")
def api_plan():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    plan = get_user_plan(session["user"])
    p = PLANS[plan]
    return jsonify({
        "plan": plan,
        "plan_name": p["name"],
        "price": p["price"],
        "credits_per_day": p["credits_per_day"],
        "features": p["features"],
        "models": {
            "chat": get_chat_model(plan),
            "code": get_code_model(plan),
            "image": get_image_provider(plan)
        },
        "credit_costs": PLAN_CREDIT_COSTS.get(plan, {})
    })


@app.route("/bookmarks")
def bookmarks_page():
    if "user" not in session: return redirect("/login")
    plan = get_user_plan(session["user"])
    conn = get_db(); c = conn.cursor()
    c.execute("""SELECT b.id,b.note,b.created_at,ch.user_msg,ch.bot_msg,ch.conversation_id
                 FROM bookmarks b JOIN chats ch ON b.chat_id=ch.id
                 WHERE b.username=%s ORDER BY b.id DESC""", (session["user"],))
    bms = c.fetchall()
    c.execute("SELECT id,title,mode,pinned FROM conversations WHERE username=%s ORDER BY pinned DESC,id DESC", (session["user"],))
    convs = c.fetchall()
    c.execute("SELECT id,title,created_at FROM notes WHERE username=%s ORDER BY id DESC LIMIT 20", (session["user"],))
    notes_list = c.fetchall(); conn.close()
    return render_template("bookmarks.html", bookmarks=bms, conversations=convs,
                           notes_list=notes_list, plan=plan, plans=PLANS, username=session["user"])


@app.route("/api/bookmark/<int:bm_id>/delete", methods=["POST"])
def delete_bookmark(bm_id):
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM bookmarks WHERE id=%s AND username=%s", (bm_id, session["user"]))
    conn.commit(); conn.close()
    return jsonify({"ok":True})


# ════════════════════════════════════════════
#  PASSWORD RESET
# ════════════════════════════════════════════

@app.route("/forgot-password", methods=["GET","POST"])
def forgot_password():
    msg = None
    if request.method == "POST":
        email = request.form.get("email","").strip()
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT username FROM users WHERE email=%s", (email,))
        row = c.fetchone()
        if row:
            token = secrets.token_urlsafe(32)
            expires = datetime.utcnow() + timedelta(hours=2)
            c.execute("INSERT INTO password_resets (username,token,expires_at) VALUES (%s,%s,%s)",
                (row[0], token, expires))
            conn.commit()
            # send_email_verification(email, token)  # reuse email sender
            print(f"[RESET] Token for {email}: /reset-password/{token}")
            msg = "If that email exists, a reset link has been sent."
        else:
            msg = "If that email exists, a reset link has been sent."
        conn.close()
    return render_template("forgot_password.html", msg=msg)


@app.route("/reset-password/<token>", methods=["GET","POST"])
def reset_password(token):
    error = None
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT username,expires_at,used FROM password_resets WHERE token=%s", (token,))
    row = c.fetchone()
    if not row or row[2] or row[1] < datetime.utcnow():
        conn.close()
        return render_template("reset_password.html", expired=True, error=None)
    if request.method == "POST":
        new_pwd = request.form.get("password","")
        if len(new_pwd) < 6:
            error = "Password must be at least 6 characters."
        else:
            hashed = bcrypt.hashpw(new_pwd.encode(), bcrypt.gensalt()).decode()
            c.execute("UPDATE users SET password=%s WHERE username=%s", (hashed, row[0]))
            c.execute("UPDATE password_resets SET used=TRUE WHERE token=%s", (token,))
            conn.commit(); conn.close()
            return redirect("/login?success=Password+reset+successfully")
    conn.close()
    return render_template("reset_password.html", expired=False, error=error, token=token)


# ════════════════════════════════════════════
#  STORIES
# ════════════════════════════════════════════

@app.route("/stories")
def get_stories_page():
    if "user" not in session: return redirect("/login")
    return redirect("/") # integrated into dashboard

@app.route("/search")
def search():
    if "user" not in session: return jsonify({"results": []})
    q = request.args.get("q", "").strip()
    if not q: return jsonify({"results": []})
    conn = get_db(); c = conn.cursor()
    c.execute("""SELECT conversation_id, user_msg, bot_plain 
                 FROM chats 
                 WHERE username=%s AND (user_msg ILIKE %s OR bot_plain ILIKE %s) 
                 ORDER BY created_at DESC LIMIT 20""",
              (session["user"], f"%{q}%", f"%{q}%"))
    rows = c.fetchall()
    results = []
    for r in rows:
        snippet = r[1] if q.lower() in r[1].lower() else (r[2] or "")[:100]
        results.append({"conv_id": r[0], "snippet": snippet})
    conn.close()
    return jsonify({"results": results})


@app.route("/api/stories")
def api_get_stories():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    conn = get_db(); c = conn.cursor()
    c.execute("""SELECT s.id, s.username, s.content, s.media_url, s.created_at, u.avatar_url
                 FROM stories s JOIN users u ON s.username = u.username
                 ORDER BY s.created_at DESC LIMIT 20""")
    rows = c.fetchall(); conn.close()
    res = []
    for r in rows:
        res.append({
            "id": r[0], "username": r[1], "content": r[2],
            "media_url": r[3], "created_at": r[4].isoformat(), "avatar": r[5]
        })
    return jsonify({"ok":True, "stories":res})

@app.route("/add_story", methods=["POST"])
def add_story():
    if "user" not in session: return redirect("/login")
    content = request.form.get("content","").strip()[:500]
    media   = request.form.get("media_url","").strip()
    if not content and not media: return redirect("/?error=Empty+story")
    
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO stories (username, content, media_url) VALUES (%s,%s,%s)",
        (session["user"], content, media))
    conn.commit(); conn.close()
    return redirect("/?success=Story+posted")


# ════════════════════════════════════════════
#  FEEDBACK
# ════════════════════════════════════════════

@app.route("/api/feedback", methods=["POST"])
def submit_feedback():
    if "user" not in session: return jsonify({"error":"Unauthorized"}),401
    body = request.get_json(force=True)
    rating  = body.get("rating", 5)
    message = body.get("message","").strip()[:1000]
    page    = body.get("page","")
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO feedback (username,rating,message,page) VALUES (%s,%s,%s,%s)",
        (session["user"], rating, message, page))
    conn.commit(); conn.close()
    return jsonify({"ok":True})


# ════════════════════════════════════════════
#  ADMIN / USAGE STATS (self-service)
# ════════════════════════════════════════════

@app.route("/stats")
def stats_page():
    if "user" not in session: return redirect("/login")
    plan = get_user_plan(session["user"])
    conn = get_db(); c = conn.cursor()
    # 7-day message count
    c.execute("""SELECT DATE(created_at) as day, COUNT(*) as cnt
                 FROM chats WHERE username=%s AND created_at > NOW()-INTERVAL '7 days'
                 GROUP BY day ORDER BY day""", (session["user"],))
    daily = c.fetchall()
    # Model breakdown
    c.execute("""SELECT model_used, COUNT(*) FROM chats WHERE username=%s AND model_used!=''
                 GROUP BY model_used ORDER BY COUNT(*) DESC""", (session["user"],))
    models = c.fetchall()
    # Total tokens
    c.execute("SELECT COALESCE(SUM(tokens_used),0) FROM chats WHERE username=%s", (session["user"],))
    total_tokens = c.fetchone()[0]
    # Notes count
    c.execute("SELECT COUNT(*) FROM notes WHERE username=%s", (session["user"],))
    total_notes = c.fetchone()[0]
    conn.close()
    return render_template("stats.html", daily=daily, models=models,
        total_tokens=total_tokens, total_notes=total_notes,
        plan=plan, plans=PLANS, username=session["user"])


# ════════════════════════════════════════════
#  HEALTH CHECK
# ════════════════════════════════════════════

@app.route("/health")
def health():
    return jsonify({"status":"ok","version":"2.0","timestamp":datetime.utcnow().isoformat()})


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /api/\nDisallow: /admin/\n",
        mimetype="text/plain")


# FIX #11: init_db() must run whether started directly OR via gunicorn.
try:
    if DATABASE_URL:
        init_db()
    else:
        print("[STARTUP] Warning: DATABASE_URL not found in environment.")
except Exception as _init_err:
    print(f"[STARTUP] init_db failed: {_init_err}")

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)