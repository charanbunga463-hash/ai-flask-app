from flask import Flask, request, render_template, redirect, session, Response, stream_with_context
import sqlite3
import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "users.db")

def get_db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

@app.route("/")
def home():
    if "user" not in session:
        return redirect("/login")
    return render_template("dashboard.html", chat=[])

# ✅ PURE STREAM (NO MODIFICATION)
@app.route("/stream")
def stream():
    user_input = request.args.get("input")

    def generate():
        full_text = ""

        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "user", "content": user_input}
            ],
            stream=True
        )

        for chunk in response:
            token = chunk.choices[0].delta.content or ""
            if token:
                full_text += token
                yield f"data: {token}\n\n"

        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache"}
    )

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        session["user"] = request.form.get("username")
        return redirect("/")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

if __name__ == "__main__":
    app.run(debug=True, threaded=True)