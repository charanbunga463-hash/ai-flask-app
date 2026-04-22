from flask import Flask, request, render_template, redirect, session, jsonify, Response, stream_with_context
import psycopg2
import os
from dotenv import load_dotenv
import markdown
from openai import OpenAI
from PyPDF2 import PdfReader
import bcrypt
import json

# ---------- ENV ----------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret")

DATABASE_URL = os.getenv("DATABASE_URL")

# ---------- AI ----------
client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

def query_ai_stream(prompt):
    """Generates a stream of tokens from the AI model."""
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Answer clearly and briefly."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=400,
            stream=True  # ✅ Enable streaming
        )
        for chunk in response:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    except Exception as e:
        yield f"Error: {str(e)}"

def query_ai(prompt):
    """Fallback non-streaming query function."""
    try:
        res = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Answer clearly and briefly."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=400
        )
        return res.choices[0].message.content.strip()
    except Exception as e:
        return f"Error: {str(e)}"

def generate_chat_title(message):
    try:
        res = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Generate a short title (max 5 words)."},
                {"role": "user", "content": message}
            ],
            max_tokens=20
        )
        return res.choices[0].message.content.strip()
    except:
        return message[:30]

def generate_image(prompt):
    return f"https://image.pollinations.ai/prompt/{prompt.replace(' ', '%20')}"

# ---------- DB ----------
def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE,
        password TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS conversations(
        id SERIAL PRIMARY KEY,
        username TEXT,
        title TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS chats(
        id SERIAL PRIMARY KEY,
        conversation_id INTEGER,
        username TEXT,
        user_msg TEXT,
        bot_msg TEXT,
        type TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS stories(
        id SERIAL PRIMARY KEY,
        username TEXT NOT NULL,
        content TEXT,
        media_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS story_views(
        id SERIAL PRIMARY KEY,
        story_id INTEGER,
        viewer TEXT,
        viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    conn.commit()
    conn.close()

init_db()

# ---------- HOME ----------
@app.route("/")
def home():
    if "user" not in session:
        return redirect("/login")

    session.pop("conv_id", None)

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT id, title FROM conversations WHERE username=%s ORDER BY id DESC",
              (session["user"],))
    conversations = c.fetchall()
    conn.close()

    return render_template("dashboard.html",
                           chat=[],
                           conversations=conversations,
                           active_chat=None)

# ---------- NEW CHAT ----------
@app.route("/new_chat")
def new_chat():
    session.pop("conv_id", None)
    return redirect("/")

# ---------- OPEN CHAT ----------
@app.route("/chat/<int:conv_id>")
def open_chat(conv_id):
    if "user" not in session:
        return redirect("/login")

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT id FROM conversations WHERE id=%s AND username=%s",
              (conv_id, session["user"]))
    if not c.fetchone():
        return redirect("/")

    session["conv_id"] = conv_id

    c.execute("""
        SELECT user_msg, bot_msg, type
        FROM chats
        WHERE conversation_id=%s
        ORDER BY id
    """, (conv_id,))
    rows = c.fetchall()

    c.execute("SELECT id, title FROM conversations WHERE username=%s ORDER BY id DESC",
              (session["user"],))
    conversations = c.fetchall()

    conn.close()

    chat = [{
        "type": r[2],
        "user": r[0],
        "bot": r[1] if r[2] == "text" else None,
        "image": r[1] if r[2] == "image" else None
    } for r in rows]

    return render_template("dashboard.html",
                           chat=chat,
                           conversations=conversations,
                           active_chat=conv_id)

# ---------- SEARCH ----------
@app.route("/search")
def search():
    if "user" not in session:
        return {"results": []}

    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return {"results": []}

    conn = get_db()
    c = conn.cursor()

    c.execute("""
        SELECT id, conversation_id, user_msg, bot_msg
        FROM chats
        WHERE username=%s
        AND (user_msg ILIKE %s OR bot_msg ILIKE %s)
        ORDER BY id DESC
        LIMIT 30
    """, (session["user"], f"%{q}%", f"%{q}%"))

    rows = c.fetchall()
    conn.close()

    results = []
    for r in rows:
        full_text = (r[2] or "") + " " + (r[3] or "")
        idx = full_text.lower().find(q.lower())
        if idx == -1:
            idx = 0

        snippet = full_text[max(0, idx - 40): idx + 40]

        results.append({
            "conv_id": r[1],
            "msg_id": r[0],
            "snippet": snippet
        })

    return {"results": results}

# ---------- STREAMING TOOL ----------
# ---------- STREAMING TOOL ----------
@app.route("/stream")
def stream():
    if "user" not in session:
        return Response("Unauthorized", status=401)

    # Get prompt from session and then clear it
    prompt = session.pop("pending_prompt", "Hello")
    conv_id = session.get("conv_id")

    def generate():
        full_response = []
        for chunk in query_ai_stream(prompt):
            full_response.append(chunk)
            yield f"data: {json.dumps({'chunk': chunk})}\n\n"
        
        complete_text = "".join(full_response)
        formatted = markdown.markdown(complete_text)
        
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO chats (conversation_id, username, user_msg, bot_msg, type)
            VALUES (%s, %s, %s, %s, %s)
        """, (conv_id, session["user"], prompt, formatted, "text"))
        conn.commit()
        conn.close()
        
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

# ---------- TOOL (FIXED FOR LARGE PDFS) ----------
@app.route("/tool", methods=["POST"])
def tool():
    if "user" not in session:
        return redirect("/login")

    user_input = request.form.get("input", "")
    file = request.files.get("file")
    conn = get_db()
    c = conn.cursor()
    conv_id = session.get("conv_id")

    if not conv_id:
        title = generate_chat_title(user_input or "New Chat")
        c.execute("INSERT INTO conversations (username, title) VALUES (%s, %s) RETURNING id",
                  (session["user"], title))
        conv_id = c.fetchone()[0]
        session["conv_id"] = conv_id

    extracted_text = ""
    if file and file.filename.endswith(".pdf"):
        try:
            reader = PdfReader(file)
            for page in reader.pages:
                extracted_text += page.extract_text() or ""
        except Exception as e:
            extracted_text = f"\n[Error reading PDF: {str(e)}]"

    final_prompt = f"{user_input}\n\n{extracted_text}".strip()

    if user_input and any(k in user_input.lower() for k in ["image","draw","photo"]) and not file:
        img = generate_image(user_input)
        c.execute("INSERT INTO chats (conversation_id, username, user_msg, bot_msg, type) VALUES (%s, %s, %s, %s, %s)",
                  (conv_id, session["user"], user_input, img, "image"))
        conn.commit()
        conn.close()
        return redirect(f"/chat/{conv_id}")

    # STORE IN SESSION INSTEAD OF URL
    session["pending_prompt"] = final_prompt
    conn.commit()
    conn.close()
    
    # Redirect with a trigger flag instead of the full text
    return redirect(f"/chat/{conv_id}?do_stream=true")

# ---------- STORIES ----------
@app.route("/add_story", methods=["POST"])
def add_story():
    if "user" not in session:
        return redirect("/login")

    content = request.form.get("content", "")
    media_url = request.form.get("media_url", "")

    conn = get_db()
    c = conn.cursor()

    c.execute("""
        INSERT INTO stories (username, content, media_url, expires_at)
        VALUES (%s, %s, %s, NOW() + INTERVAL '24 HOURS')
    """, (session["user"], content, media_url))

    conn.commit()
    conn.close()

    return redirect("/")

@app.route("/stories")
def get_stories():
    if "user" not in session:
        return jsonify([])

    conn = get_db()
    c = conn.cursor()

    c.execute("""
        SELECT id, username, content, media_url, created_at
        FROM stories
        WHERE expires_at > NOW()
        ORDER BY created_at DESC
    """)

    rows = c.fetchall()
    conn.close()

    return jsonify([{
        "id": r[0],
        "user": r[1],
        "content": r[2],
        "media": r[3],
        "created_at": str(r[4])
    } for r in rows])

# ---------- STORY VIEW TRACK ----------
@app.route("/view_story/<int:story_id>")
def view_story(story_id):
    if "user" not in session:
        return jsonify({"status": "error"})

    conn = get_db()
    c = conn.cursor()

    c.execute("""
        INSERT INTO story_views (story_id, viewer)
        VALUES (%s, %s)
    """, (story_id, session["user"]))

    conn.commit()
    conn.close()

    return jsonify({"status": "ok"})

# ---------- DELETE CHAT ----------
@app.route("/delete_chat/<int:conv_id>", methods=["POST"])
def delete_chat(conv_id):
    if "user" not in session:
        return redirect("/login")

    conn = get_db()
    c = conn.cursor()

    # Delete all messages in the conversation first (Foreign Key cleanup)
    c.execute("DELETE FROM chats WHERE conversation_id=%s AND username=%s", (conv_id, session["user"]))
    # Delete the conversation itself
    c.execute("DELETE FROM conversations WHERE id=%s AND username=%s", (conv_id, session["user"]))

    conn.commit()
    conn.close()
    
    # If the deleted chat was the active one, clear the session variable
    if session.get("conv_id") == conv_id:
        session.pop("conv_id", None)

    return redirect("/")

# ---------- RENAME CHAT ----------
@app.route("/edit_chat/<int:conv_id>", methods=["POST"])
def edit_chat(conv_id):
    if "user" not in session:
        return redirect("/login")

    new_title = request.form.get("title")
    if new_title:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE conversations SET title=%s WHERE id=%s AND username=%s", 
                  (new_title, conv_id, session["user"]))
        conn.commit()
        conn.close()

    return redirect("/")

# ---------- AUTH (SECURE) ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        user = request.form.get("username")
        pwd = request.form.get("password")

        conn = get_db()
        c = conn.cursor()

        c.execute("SELECT password FROM users WHERE username=%s", (user,))
        data = c.fetchone()

        if data:
            stored = data[0]

            if stored.startswith("$2b$"):
                valid = bcrypt.checkpw(pwd.encode(), stored.encode())
            else:
                valid = (pwd == stored)
                if valid:
                    new_hash = bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()
                    c.execute("UPDATE users SET password=%s WHERE username=%s", (new_hash, user))
                    conn.commit()

            if valid:
                session["user"] = user
                conn.close()
                return redirect("/")

        conn.close()
        error = "Invalid credentials"

    return render_template("login.html", error=error)

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None

    if request.method == "POST":
        user = request.form.get("username")
        pwd = request.form.get("password")

        try:
            conn = get_db()
            c = conn.cursor()

            hashed_pwd = bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()

            c.execute("INSERT INTO users (username, password) VALUES (%s, %s)",
                      (user, hashed_pwd))

            conn.commit()
            conn.close()

            return redirect("/login")
        except:
            error = "Username exists"

    return render_template("register.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ---------- RUN ----------
if __name__ == "__main__":
    app.run(debug=True)