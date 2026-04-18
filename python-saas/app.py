from flask import Flask, request, render_template, redirect, session
import sqlite3
import os
from dotenv import load_dotenv

# Load env
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret")

# ---------------- AI SETUP (GROQ) ----------------
from openai import OpenAI

client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

def query_ai(prompt):
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Error: {str(e)}"

# ---------------- DATABASE ----------------
def init_db():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT UNIQUE,
                  password TEXT)''')
    conn.commit()
    conn.close()

init_db()

# ---------------- ROUTES ----------------

@app.route("/")
def home():
    if "user" in session:
        return render_template("dashboard.html", user=session["user"], count="Unlimited")
    return redirect("/login")

# LOGIN
@app.route("/login", methods=["GET", "POST"])
def login():
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
        return "Invalid login"

    return render_template("login.html")

# REGISTER
@app.route("/register", methods=["GET", "POST"])
def register():
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
            return "Username already exists"

    return render_template("register.html")

# TOOL (NO LIMIT)
@app.route("/tool", methods=["POST"])
def tool():
    if "user" not in session:
        return redirect("/login")

    user = session["user"]
    user_input = request.form.get("input")

    result = query_ai(f"Answer clearly: {user_input}")

    return render_template("dashboard.html", user=user, output=result, count="Unlimited")

# LOGOUT
@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/login")

# RUN
if __name__ == "__main__":
    app.run(debug=True)
