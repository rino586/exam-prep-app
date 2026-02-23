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
async def login_post(request: Request):
    form_data = await request.form()
    password = str(form_data.get("password", ""))
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
            passage TEXT NOT NULL DEFAULT '',
            passage_type TEXT NOT NULL DEFAULT '',
            problem_text TEXT NOT NULL,
            problem_figure TEXT NOT NULL DEFAULT '',
            answer TEXT NOT NULL,
            steps TEXT NOT NULL DEFAULT '',
            hint TEXT NOT NULL DEFAULT '',
            hint_figure TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (original_problem_id) REFERENCES problems(id)
        );
    """)
    # 既存DBにカラムがない場合は追加
    for col in ["steps", "problem_figure", "hint_figure", "passage", "passage_type"]:
        try:
            conn.execute(f"ALTER TABLE generated_problems ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
    conn.commit()
    conn.close()


# ---- AI ----

def get_claude_client():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません")
    return anthropic.Anthropic(api_key=api_key)


MATH_JSON_FORMAT = """{
  "problems": [
    {
      "problem": "問題文",
      "problem_figure": "問題に付ける図・表（元の問題に図がある場合は必ず作成。不要なら空文字）",
      "answer": "答え（例：30km/h）",
      "steps": "計算式と手順（例：速さ = 距離 ÷ 時間\\n= 120 ÷ 4\\n= 30(km/h)）",
      "hint": "考え方のヒント",
      "hint_figure": "ヒントに付ける図・表（解き方の補足に役立つ場合のみ。不要なら空文字）"
    }
  ]
}"""

JAPANESE_JSON_FORMAT = """{
  "problems": [
    {
      "passage": "読解問題の場合：300〜500字の物語文または説明文。読解問題でない場合は空文字",
      "passage_type": "物語文／説明文／随筆／漢字／語彙／文法 など問題の種類",
      "problem": "設問文（例：「線部①『〜』とはどういう意味ですか」など）",
      "problem_figure": "文の構造図・語の関係図など（必要な場合のみ。不要なら空文字）",
      "answer": "答え",
      "steps": "解き方の手順（例：①〜に注目\\n②〜から判断\\n答え：〇〇）",
      "hint": "考え方のヒント",
      "hint_figure": "ヒントの補足図（不要なら空文字）"
    }
  ]
}"""

MATH_NOTES = """【難易度の基準：偏差値50レベルの中学受験（中堅校）】
- 速さ・割合・比・平面図形・基本的な規則性など標準的な単元から出題する
- 複雑な場合分けや高度な特殊算（つるかめ算の発展・複雑な旅人算など）は使わない
- 計算は整数・簡単な分数・小数の範囲で収める
- 1〜2ステップで解ける問題にする（3ステップ以上の複雑な問題は避ける）
- 数値や条件を変えて、元の問題と同じ解き方で解けるようにする

【problem_figureフィールドのルール（最重要）】
- 元の問題に図・表・グラフ・数直線が含まれる場合は【必ず】SVG形式で新しい図を作成する
- SVGは必ず1行（改行なし）で出力し、属性値はすべてシングルクォート(')を使う
- viewBox='0 0 300 200' を基本サイズとする
- 元の問題に図がない純粋な計算問題は空文字にする

【SVGテンプレート集（数値・ラベルを変えて使うこと）】

■ 長方形（面積・周囲の問題）:
<svg viewBox='0 0 300 200' xmlns='http://www.w3.org/2000/svg'><rect x='50' y='40' width='180' height='100' fill='#dbeafe' stroke='#1d4ed8' stroke-width='2'/><text x='140' y='30' text-anchor='middle' font-size='16' fill='#1d4ed8'>8cm</text><text x='250' y='95' text-anchor='start' font-size='16' fill='#1d4ed8'>5cm</text></svg>

■ 直角三角形:
<svg viewBox='0 0 300 200' xmlns='http://www.w3.org/2000/svg'><polygon points='50,160 50,40 230,160' fill='#dbeafe' stroke='#1d4ed8' stroke-width='2'/><rect x='50' y='143' width='17' height='17' fill='none' stroke='#1d4ed8' stroke-width='1.5'/><text x='140' y='185' text-anchor='middle' font-size='16' fill='#1d4ed8'>12cm</text><text x='20' y='105' text-anchor='middle' font-size='16' fill='#1d4ed8'>9cm</text></svg>

■ 速さ・距離の図:
<svg viewBox='0 0 300 120' xmlns='http://www.w3.org/2000/svg'><line x1='30' y1='60' x2='260' y2='60' stroke='#333' stroke-width='2'/><polygon points='260,55 275,60 260,65' fill='#333'/><circle cx='30' cy='60' r='5' fill='#1d4ed8'/><circle cx='260' cy='60' r='5' fill='#e11d48'/><text x='30' y='85' text-anchor='middle' font-size='14' fill='#1d4ed8'>A地点</text><text x='260' y='85' text-anchor='middle' font-size='14' fill='#e11d48'>B地点</text><text x='145' y='45' text-anchor='middle' font-size='15' fill='#333'>120km</text></svg>

■ 表（行き帰りなど）:
<svg viewBox='0 0 300 160' xmlns='http://www.w3.org/2000/svg'><rect x='20' y='20' width='260' height='120' fill='none' stroke='#333' stroke-width='1.5'/><line x1='100' y1='20' x2='100' y2='140' stroke='#333' stroke-width='1.5'/><line x1='200' y1='20' x2='200' y2='140' stroke='#333' stroke-width='1.5'/><line x1='20' y1='60' x2='280' y2='60' stroke='#333' stroke-width='1.5'/><line x1='20' y1='100' x2='280' y2='100' stroke='#333' stroke-width='1.5'/><text x='60' y='46' text-anchor='middle' font-size='14'></text><text x='150' y='46' text-anchor='middle' font-size='14'>行き</text><text x='240' y='46' text-anchor='middle' font-size='14'>帰り</text><text x='60' y='86' text-anchor='middle' font-size='14'>速さ</text><text x='150' y='86' text-anchor='middle' font-size='14'>60km/h</text><text x='240' y='86' text-anchor='middle' font-size='14'>40km/h</text><text x='60' y='126' text-anchor='middle' font-size='14'>時間</text><text x='150' y='126' text-anchor='middle' font-size='14'>□時間</text><text x='240' y='126' text-anchor='middle' font-size='14'>□時間</text></svg>

【stepsフィールドの書き方】
- 計算の各ステップを\\nで区切って記載、単位を必ず書く

【hint_figureフィールド】
- 不要な場合は空文字にする"""

JAPANESE_NOTES = """【難易度の基準：偏差値50レベルの中学受験（中堅校）】
- 小学6年生までの漢字、基本的な慣用句・ことわざ・四字熟語（教科書頻出レベル）
- 文の組み立て・接続詞・品詞など基本的な文法
- 答えが一つに決まる問題にする

【passageフィールドのルール（最重要）】
- 元の問題が読解問題（物語文・説明文・随筆などの本文付き）の場合は【必ず】新しい文章を書く
- 文章は300〜500字、小学6年生が読める語彙レベルにする
- 物語文なら登場人物・情景・心情を含む自然な文章にする
- 説明文なら自然・科学・社会などのテーマで論理的な構成にする
- 読解問題でない（漢字・語彙・文法単独の問題）場合は空文字にする

【passage_typeフィールド】
- 「物語文」「説明文」「随筆」「漢字」「語彙」「文法」「慣用句」など具体的に記載

【problem_figureフィールドのルール】
- 元の問題に図・表・文の構造図などが含まれている場合は【必ず】新しい図を作成する
- 文の構造を問う問題：「主語→述語→修飾語」のような図
- 語の関係を問う問題：「上位語─下位語」などの関係図
- 元の問題に図がない場合は空文字にする

【stepsフィールドの書き方】
- 解き方の手順を\\nで区切って記載（例：「①〜に注目\\n②〜から判断\\n答え：〇〇」）

【hint_figureフィールドの書き方】
- ヒントの補足として図が役立つ場合のみ記載
- 不要な場合は空文字にする"""


def _json_format(subject: str) -> str:
    return MATH_JSON_FORMAT if subject == "算数" else JAPANESE_JSON_FORMAT


def build_text_prompt(subject: str, problem: str) -> str:
    if subject == "算数":
        base = f"あなたは中学受験専門の算数の先生です。\n以下の間違えた問題と同じ種類・難易度の類似問題を3問作ってください。\n元の間違えた問題: {problem}\n\n{MATH_NOTES}"
    else:
        base = f"あなたは中学受験専門の国語の先生です。\n以下の間違えた問題と同じ種類・難易度の類似問題を3問作ってください。\n元の間違えた問題: {problem}\n\n{JAPANESE_NOTES}"
    return base + f"\n\n以下のJSON形式のみで返してください（他のテキスト不要）:\n{_json_format(subject)}"


def build_image_prompt(subject: str) -> str:
    if subject == "算数":
        base = f"あなたは中学受験専門の算数の先生です。\n画像に写っている問題を読み取り、同じ種類・難易度の類似問題を3問作ってください。\n\n{MATH_NOTES}"
    else:
        base = f"あなたは中学受験専門の国語の先生です。\n画像に写っている問題を読み取り、同じ種類・難易度の類似問題を3問作ってください。\n\n{JAPANESE_NOTES}"
    return base + f"\n\n以下のJSON形式のみで返してください（他のテキスト不要）:\n{_json_format(subject)}"


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
            "INSERT INTO generated_problems (original_problem_id, passage, passage_type, problem_text, problem_figure, answer, steps, hint, hint_figure, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (problem_id, p.get("passage", ""), p.get("passage_type", ""), p["problem"], p.get("problem_figure", ""), p["answer"], p.get("steps", ""), p.get("hint", ""), p.get("hint_figure", ""), datetime.now().strftime("%Y-%m-%d %H:%M"))
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
async def generate_problems(request: Request):
    if not is_authenticated(request):
        return login_redirect()

    form_data = await request.form()
    subject = str(form_data.get("subject", "算数"))
    problem = str(form_data.get("problem", ""))
    image_data = str(form_data.get("image_data", ""))

    use_image = bool(image_data.strip())
    if not use_image and not problem.strip():
        keys = list(form_data.keys())
        return templates.TemplateResponse("input_problem.html", {
            "request": request,
            "error": f"[デバッグ] 受信したフィールド: {keys} / subject='{subject}' / problem文字数={len(problem)}"
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
