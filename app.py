import os
import sqlite3
import json
import base64
import re
from datetime import datetime
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
import anthropic
import uvicorn

app = FastAPI(title="受験対策アプリ")
templates = Jinja2Templates(directory="templates")
templates.env.globals["enumerate"] = enumerate

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "change-this-secret-key-in-production")
)

app.mount("/static", StaticFiles(directory="static"), name="static")

DATABASE_PATH = os.getenv("DATABASE_PATH", "exam_prep.db")


# ---- 認証ヘルパー ----

def is_authenticated(request: Request) -> bool:
    return request.session.get("authenticated") is True


def login_redirect() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=302)


# ---- ログイン / ログアウト ----

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, password: str = Form(...)):
    app_password = os.getenv("APP_PASSWORD", "")
    if not app_password:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "パスワードが設定されていません。環境変数 APP_PASSWORD を設定してください。"
        })
    if password == app_password:
        request.session["authenticated"] = True
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": "パスワードが違います。もう一度入力してください。"
    })


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# ---- DB ----

def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS problems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            original_problem TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS generated_problems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_problem_id INTEGER NOT NULL,
            problem_text TEXT NOT NULL,
            answer TEXT NOT NULL,
            hint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (original_problem_id) REFERENCES problems(id)
        );
    """)
    conn.commit()
    conn.close()


# ---- AI ----

def get_claude_client():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません")
    return anthropic.Anthropic(api_key=api_key)


JSON_FORMAT = """{
  "problems": [
    {"problem": "問題文", "answer": "答え", "hint": "考え方のヒント"},
    {"problem": "問題文", "answer": "答え", "hint": "考え方のヒント"},
    {"problem": "問題文", "answer": "答え", "hint": "考え方のヒント"}
  ]
}"""

MATH_NOTES = """【難易度の基準：偏差値50レベルの中学受験（中堅校）】
- 速さ・割合・比・平面図形・基本的な規則性など標準的な単元から出題する
- 複雑な場合分けや高度な特殊算（つるかめ算の発展・複雑な旅人算など）は使わない
- 計算は整数・簡単な分数・小数の範囲で収める
- 1〜2ステップで解ける問題にする（3ステップ以上の複雑な問題は避ける）
- 数値や条件を変えて、元の問題と同じ解き方で解けるようにする"""

JAPANESE_NOTES = """【難易度の基準：偏差値50レベルの中学受験（中堅校）】
- 物語文・説明文の読解（文章量は200〜400字程度）、小学6年生までの漢字
- 基本的な慣用句・ことわざ・四字熟語（教科書頻出レベル）
- 文の組み立て・接続詞・品詞など基本的な文法
- 難解な文語体や抽象的すぎる文章は使わない
- 答えが一つに決まる問題にする"""


def build_text_prompt(subject: str, problem: str) -> str:
    if subject == "算数":
        base = f"あなたは中学受験専門の算数の先生です。\n以下の間違えた問題と同じ種類・難易度の類似問題を3問作ってください。\n元の間違えた問題: {problem}\n\n{MATH_NOTES}"
    else:
        base = f"あなたは中学受験専門の国語の先生です。\n以下の間違えた問題と同じ種類・難易度の類似問題を3問作ってください。\n元の間違えた問題: {problem}\n\n{JAPANESE_NOTES}"
    return base + f"\n\n以下のJSON形式のみで返してください（他のテキスト不要）:\n{JSON_FORMAT}"


def build_image_prompt(subject: str) -> str:
    if subject == "算数":
        base = f"あなたは中学受験専門の算数の先生です。\n画像に写っている問題を読み取り、同じ種類・難易度の類似問題を3問作ってください。\n\n{MATH_NOTES}"
    else:
        base = f"あなたは中学受験専門の国語の先生です。\n画像に写っている問題を読み取り、同じ種類・難易度の類似問題を3問作ってください。\n\n{JAPANESE_NOTES}"
    return base + f"\n\n以下のJSON形式のみで返してください（他のテキスト不要）:\n{JSON_FORMAT}"


def extract_image_data(data_url: str) -> tuple[str, str]:
    match = re.match(r"data:([^;]+);base64,(.+)", data_url, re.DOTALL)
    if not match:
        raise ValueError("無効な画像データです")
    return match.group(1), match.group(2)


def parse_generated(response_text: str) -> list:
    start = response_text.find('{')
    end = response_text.rfind('}') + 1
    if start == -1 or end == 0:
        raise ValueError("JSONが見つかりませんでした")
    return json.loads(response_text[start:end])["problems"]


def save_generated(subject: str, original_label: str, generated: list) -> int:
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO problems (subject, original_problem, created_at) VALUES (?, ?, ?)",
        (subject, original_label, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    problem_id = cursor.lastrowid
    for p in generated:
        conn.execute(
            "INSERT INTO generated_problems (original_problem_id, problem_text, answer, hint, created_at) VALUES (?, ?, ?, ?, ?)",
            (problem_id, p["problem"], p["answer"], p["hint"], datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
    conn.commit()
    conn.close()
    return problem_id


# ---- ルート ----

@app.on_event("startup")
async def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not is_authenticated(request):
        return login_redirect()
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as cnt FROM problems").fetchone()["cnt"]
    conn.close()
    return templates.TemplateResponse("index.html", {"request": request, "total": total})


@app.get("/input", response_class=HTMLResponse)
async def input_page(request: Request):
    if not is_authenticated(request):
        return login_redirect()
    return templates.TemplateResponse("input_problem.html", {"request": request})


@app.post("/generate", response_class=HTMLResponse)
async def generate_problems(
    request: Request,
    subject: str = Form(...),
    problem: str = Form(""),
    image_data: str = Form("")
):
    if not is_authenticated(request):
        return login_redirect()

    use_image = bool(image_data.strip())
    if not use_image and not problem.strip():
        return templates.TemplateResponse("input_problem.html", {
            "request": request,
            "error": "問題を入力するか、画像をアップロードしてください。"
        })

    try:
        client = get_claude_client()
    except ValueError:
        return templates.TemplateResponse("input_problem.html", {
            "request": request,
            "error": "APIキーが設定されていません。環境変数 ANTHROPIC_API_KEY を確認してください。"
        })

    try:
        if use_image:
            media_type, b64_data = extract_image_data(image_data.strip())
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64_data
                            }
                        },
                        {"type": "text", "text": build_image_prompt(subject)}
                    ]
                }]
            )
            original_label = "（画像で入力された問題）"
        else:
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{"role": "user", "content": build_text_prompt(subject, problem.strip())}]
            )
            original_label = problem.strip()

        generated = parse_generated(message.content[0].text.strip())
        problem_id = save_generated(subject, original_label, generated)

        return templates.TemplateResponse("result.html", {
            "request": request,
            "subject": subject,
            "original": original_label,
            "image_data": image_data.strip() if use_image else "",
            "problems": generated,
            "problem_id": problem_id
        })

    except (json.JSONDecodeError, ValueError, KeyError):
        return templates.TemplateResponse("input_problem.html", {
            "request": request,
            "error": "問題の生成に失敗しました。もう一度お試しください。"
        })
    except Exception as e:
        return templates.TemplateResponse("input_problem.html", {
            "request": request,
            "error": f"エラーが発生しました: {str(e)}"
        })


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    if not is_authenticated(request):
        return login_redirect()
    conn = get_db()
    rows = conn.execute("""
        SELECT p.id, p.subject, p.original_problem, p.created_at,
               COUNT(gp.id) as generated_count
        FROM problems p
        LEFT JOIN generated_problems gp ON p.id = gp.original_problem_id
        GROUP BY p.id
        ORDER BY p.created_at DESC
    """).fetchall()
    conn.close()
    return templates.TemplateResponse("history.html", {"request": request, "problems": rows})


@app.get("/practice/{problem_id}", response_class=HTMLResponse)
async def practice_page(request: Request, problem_id: int):
    if not is_authenticated(request):
        return login_redirect()
    conn = get_db()
    original = conn.execute("SELECT * FROM problems WHERE id = ?", (problem_id,)).fetchone()
    if not original:
        conn.close()
        raise HTTPException(status_code=404, detail="問題が見つかりません")
    problems = conn.execute(
        "SELECT * FROM generated_problems WHERE original_problem_id = ?", (problem_id,)
    ).fetchall()
    conn.close()
    return templates.TemplateResponse("practice.html", {
        "request": request,
        "original": original,
        "problems": problems
    })


@app.post("/delete/{problem_id}")
async def delete_problem(request: Request, problem_id: int):
    if not is_authenticated(request):
        return login_redirect()
    conn = get_db()
    conn.execute("DELETE FROM generated_problems WHERE original_problem_id = ?", (problem_id,))
    conn.execute("DELETE FROM problems WHERE id = ?", (problem_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"success": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
