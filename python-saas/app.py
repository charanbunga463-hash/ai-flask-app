from flask import Flask, request, render_template, redirect, session
import sqlite3
import os
from dotenv import load_dotenv
import markdown
from openai import OpenAI

# ---------------- LOAD ENV ----------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret")

# ---------------- AI SETUP ----------------
client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

def query_ai(prompt):
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Answer only current question."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Error: {str(e)}"

# ---------------- IMAGE ----------------
def generate_image(prompt):
    prompt = prompt.replace(" ", "%20")
    return f"https://image.pollinations.ai/prompt/{prompt}"

# ---------------- DATABASE ----------------
def init_db():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        user_msg TEXT,
        bot_msg TEXT,
        type TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.commit()
    conn.close()

init_db()

# ---------------- HOME ----------------
@app.route("/")
def home():
    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT user_msg, bot_msg, type FROM chats WHERE username=? ORDER BY id ASC",
              (session["user"],))
    data = c.fetchall()
    conn.close()

    chat = []
    for row in data:
        if row[2] == "text":
            chat.append({"type": "text", "user": row[0], "bot": row[1]})
        else:
            chat.append({"type": "image", "user": row[0], "image": row[1]})

    return render_template("dashboard.html", user=session["user"], chat=chat)

# ---------------- DELETE HISTORY ----------------
@app.route("/delete_history", methods=["POST"])
def delete_history():
    if "user" not in session:
        return redirect("/login")

    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("DELETE FROM chats WHERE username=?", (session["user"],))
    conn.commit()
    conn.close()

    return redirect("/")

# ---------------- LOGIN ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        user = request.form.get("username")
        pwd = request.form.get("password")

        conn = sqlite3.connect("users.db")
        c = conn.cursor()
        c.execute("SELECT password FROM users WHERE username=?", (user,))
        data = c.fetchone()
        conn.close()

        if data and pwd == data[0]:
            session["user"] = user
            return redirect("/")
        else:
            error = "Invalid credentials"

    return render_template("login.html", error=error)

# ---------------- REGISTER ----------------
@app.route("/register", methods=["GET", "POST"])
def register():
    error = None

    if request.method == "POST":
        user = request.form.get("username")
        pwd = request.form.get("password")

        try:
            conn = sqlite3.connect("users.db")
            c = conn.cursor()
            c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (user, pwd))
            conn.commit()
            conn.close()
            return redirect("/login")
        except sqlite3.IntegrityError:
            error = "Username exists"

    return render_template("register.html", error=error)

# ---------------- TOOL ----------------
@app.route("/tool", methods=["POST"])
def tool():
    if "user" not in session:
        return redirect("/login")

    user_input = request.form.get("input")

    conn = sqlite3.connect("users.db")
    c = conn.cursor()

    # IMAGE
    if any(k in user_input.lower() for k in ["image", "draw", "picture", "photo"]):
        image_url = generate_image(user_input)

        c.execute("INSERT INTO chats (username, user_msg, bot_msg, type) VALUES (?, ?, ?, ?)",
                  (session["user"], user_input, image_url, "image"))

        conn.commit()
        conn.close()
        return redirect("/")

    # TEXT
    result = query_ai(user_input)
    formatted = markdown.markdown(result)

    c.execute("INSERT INTO chats (username, user_msg, bot_msg, type) VALUES (?, ?, ?, ?)",
              (session["user"], user_input, formatted, "text"))

    conn.commit()
    conn.close()

    return redirect("/")

# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)