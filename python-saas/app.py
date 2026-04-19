from flask import Flask, request, render_template, redirect, session
import sqlite3
import os
from dotenv import load_dotenv
import markdown

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

# ✅ FIXED: No previous context mixing
def query_ai(prompt):
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": "Answer ONLY the current question. Do NOT include previous answers or context."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Error: {str(e)}"


# ---------------- IMAGE GENERATION ----------------
def generate_image(prompt):
    prompt = prompt.replace(" ", "%20")
    return f"https://image.pollinations.ai/prompt/{prompt}"


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
        if "chat" not in session:
            session["chat"] = []

        return render_template(
            "dashboard.html",
            user=session["user"],
            chat=session["chat"],
            count="Unlimited"
        )
    return redirect("/login")


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
            session["chat"] = []  # reset chat
            return redirect("/")
        else:
            error = "Invalid username or password"

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
            error = "Username already exists"

    return render_template("register.html", error=error)


# ---------------- TOOL ----------------
@app.route("/tool", methods=["POST"])
def tool():
    if "user" not in session:
        return redirect("/login")

    if "chat" not in session:
        session["chat"] = []

    user_input = request.form.get("input")

    # -------- IMAGE --------
    keywords = ["image", "generate image", "draw", "picture", "photo"]
    if any(k in user_input.lower() for k in keywords):
        image_url = generate_image(user_input)

        session["chat"].append({
            "type": "image",
            "user": user_input,
            "image": image_url
        })

        session.modified = True
        return redirect("/")

    # -------- TEXT --------
    result = query_ai(user_input)
    formatted_output = markdown.markdown(result)

    session["chat"].append({
        "type": "text",
        "user": user_input,
        "bot": formatted_output
    })

    session.modified = True

    return redirect("/")


# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.pop("user", None)
    session.pop("chat", None)
    return redirect("/login")


# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)