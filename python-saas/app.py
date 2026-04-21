from flask import Flask, request, render_template, redirect, session
import sqlite3
import os
from dotenv import load_dotenv
import markdown
from openai import OpenAI
from PyPDF2 import PdfReader

# ---------------- ENV ----------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "users.db")

# ---------------- AI ----------------
client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

def query_ai(prompt):
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
        title = res.choices[0].message.content.strip()
        return title if title else message[:30]
    except:
        return message[:30]

def generate_image(prompt):
    return f"https://image.pollinations.ai/prompt/{prompt.replace(' ', '%20')}"

# ---------------- DB ----------------
def get_db():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS conversations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        title TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS chats(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER,
        username TEXT,
        user_msg TEXT,
        bot_msg TEXT,
        type TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    conn.commit()
    conn.close()

init_db()

# ---------------- HOME ----------------
@app.route("/")
def home():
    if "user" not in session:
        return redirect("/login")

    session.pop("conv_id", None)

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT id, title FROM conversations WHERE username=? ORDER BY id DESC",
              (session["user"],))
    conversations = c.fetchall()

    conn.close()

    return render_template("dashboard.html",
                           user=session["user"],
                           chat=[],
                           conversations=conversations,
                           active_chat=None)

# ---------------- NEW CHAT ----------------
@app.route("/new_chat")
def new_chat():
    session.pop("conv_id", None)
    return redirect("/")

# ---------------- OPEN CHAT ----------------
@app.route("/chat/<int:conv_id>")
def open_chat(conv_id):
    if "user" not in session:
        return redirect("/login")

    conn = get_db()
    c = conn.cursor()

    # validate ownership
    c.execute("SELECT id FROM conversations WHERE id=? AND username=?",
              (conv_id, session["user"]))
    if not c.fetchone():
        conn.close()
        return redirect("/")

    session["conv_id"] = conv_id

    # fetch chats
    c.execute("""
        SELECT user_msg, bot_msg, type 
        FROM chats 
        WHERE conversation_id=? 
        ORDER BY id
    """, (conv_id,))
    rows = c.fetchall()

    # fetch sidebar chats
    c.execute("SELECT id, title FROM conversations WHERE username=? ORDER BY id DESC",
              (session["user"],))
    conversations = c.fetchall()

    conn.close()

    chat = []
    for r in rows:
        chat.append({
            "type": r[2],
            "user": r[0],
            "bot": r[1] if r[2] == "text" else None,
            "image": r[1] if r[2] == "image" else None
        })

    return render_template("dashboard.html",
                           user=session["user"],
                           chat=chat,
                           conversations=conversations,
                           active_chat=conv_id)

# ---------------- DELETE CHAT ----------------
@app.route("/delete_chat/<int:conv_id>", methods=["POST"])
def delete_chat(conv_id):
    if "user" not in session:
        return redirect("/login")

    conn = get_db()
    c = conn.cursor()

    c.execute("DELETE FROM chats WHERE conversation_id=?", (conv_id,))
    c.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    conn.commit()

    c.execute("SELECT id FROM conversations WHERE username=? ORDER BY id DESC LIMIT 1",
              (session["user"],))
    next_chat = c.fetchone()

    conn.close()

    if next_chat:
        return redirect(f"/chat/{next_chat[0]}")
    else:
        session.pop("conv_id", None)
        return redirect("/")

# ---------------- EDIT CHAT ----------------
@app.route("/edit_chat/<int:conv_id>", methods=["POST"])
def edit_chat(conv_id):
    if "user" not in session:
        return redirect("/login")

    title = request.form.get("title")

    conn = get_db()
    c = conn.cursor()

    c.execute("UPDATE conversations SET title=? WHERE id=? AND username=?",
              (title, conv_id, session["user"]))

    conn.commit()
    conn.close()

    return redirect(f"/chat/{conv_id}")

# ---------------- TOOL (TEXT + FILE) ----------------
@app.route("/tool", methods=["POST"])
def tool():
    if "user" not in session:
        return redirect("/login")

    user_input = request.form.get("input", "")
    file = request.files.get("file")

    conn = get_db()
    c = conn.cursor()

    conv_id = session.get("conv_id")

    # create conversation
    if not conv_id:
        base_title = user_input or (file.filename if file else "New Chat")
        title = generate_chat_title(base_title)

        c.execute("INSERT INTO conversations (username, title) VALUES (?, ?)",
                  (session["user"], title))

        conv_id = c.lastrowid
        session["conv_id"] = conv_id

    extracted_text = ""

    # -------- PDF --------
    if file and file.filename.endswith(".pdf"):
        try:
            reader = PdfReader(file)
            for page in reader.pages:
                extracted_text += page.extract_text() or ""
        except:
            extracted_text = "Could not read PDF."

    # -------- IMAGE --------
    elif file and file.mimetype.startswith("image"):
        extracted_text = f"User uploaded image ({file.filename}). Describe it."

    # combine prompt
    final_prompt = f"{user_input}\n\n{extracted_text}".strip()

    # -------- IMAGE GENERATION --------
    if user_input and any(k in user_input.lower() for k in ["image", "draw", "photo", "picture"]) and not file:
        img = generate_image(user_input)

        c.execute("""
            INSERT INTO chats (conversation_id, username, user_msg, bot_msg, type)
            VALUES (?, ?, ?, ?, ?)
        """, (conv_id, session["user"], user_input, img, "image"))

        conn.commit()
        conn.close()
        return redirect(f"/chat/{conv_id}")

    # -------- TEXT RESPONSE --------
    result = query_ai(final_prompt)
    formatted = markdown.markdown(result)

    c.execute("""
        INSERT INTO chats (conversation_id, username, user_msg, bot_msg, type)
        VALUES (?, ?, ?, ?, ?)
    """, (conv_id, session["user"], user_input or (file.filename if file else ""), formatted, "text"))

    conn.commit()
    conn.close()

    return redirect(f"/chat/{conv_id}")

# ---------------- AUTH ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        user = request.form.get("username")
        pwd = request.form.get("password")

        conn = get_db()
        c = conn.cursor()

        c.execute("SELECT password FROM users WHERE username=?", (user,))
        data = c.fetchone()
        conn.close()

        if data and pwd == data[0]:
            session["user"] = user
            session.pop("conv_id", None)
            return redirect("/")
        else:
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

            c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (user, pwd))
            conn.commit()
            conn.close()

            return redirect("/login")

        except sqlite3.IntegrityError:
            error = "Username exists"

    return render_template("register.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)