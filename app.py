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
- 数値や条件を変えて、元の問題と同じ解き方で解けるようにする

【problem_figureフィールドのルール】
- 元の問題に図・表・グラフ・数直線が含まれる場合は【必ず】SVG形式で新しい図を作成する
- SVGは必ず1行（改行なし）で出力し、属性値はすべてシングルクォート(')を使う
- 元の問題に図がない計算問題は空文字にする

【SVGの基本ルール】
- viewBox='0 0 320 230' を標準サイズとする
- SVGは必ず1行（改行なし）で出力し、属性値はすべてシングルクォート(')を使う
- 図形の塗り: fill='#dbeafe'、輪郭: stroke='#1d4ed8' stroke-width='2'
- 点線の辺: stroke-dasharray='6,4'（補助的・仮定の辺）
- 補助線: stroke='#94a3b8' stroke-width='1.5' stroke-dasharray='5,3'
- 求める角度・辺: stroke='#dc2626' fill='none'
- 頂点ラベル（A,B,C,D など）: font-size='14' font-weight='bold' fill='#333'
- 辺の長さラベル: font-size='13' fill='#333'

【必須パーツ：必ず使うこと】

■ 頂点ラベル（全図形で必ず付ける）:
<text x='X座標' y='Y座標' font-size='14' font-weight='bold' fill='#333'>A</text>
※頂点の外側に配置（左上頂点なら x-12,y-5、右上なら x+5,y-5、左下なら x-12,y+15、右下なら x+5,y+15）

■ 直角マーク（頂点内側に14×14の小正方形）:
左下直角: <rect x='頂点X' y='頂点Y-14' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/>
右下直角: <rect x='頂点X-14' y='頂点Y-14' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/>
左上直角: <rect x='頂点X' y='頂点Y' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/>
右上直角: <rect x='頂点X-14' y='頂点Y' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/>
頂点上部の◇直角マーク: <path d='M 頂点X-10,頂点Y+10 L 頂点X,頂点Y L 頂点X+10,頂点Y+10' fill='none' stroke='#333' stroke-width='1.5'/>

■ 辺上の点（中点・分割点）:
<circle cx='点X' cy='点Y' r='3' fill='#333'/>
<text x='点X+6' y='点Y+4' font-size='13' font-weight='bold' fill='#333'>P</text>

■ 角度アーク（既知の角度）:
<path d='M 頂点X+25,頂点Y A 25,25 0 0 0 頂点X,頂点Y-25' fill='none' stroke='#333' stroke-width='1.5'/>
<text x='頂点X+20' y='頂点Y-8' font-size='12' fill='#333'>60°</text>

■ 求める角度（赤色・必ず大きく目立つように）:
<path d='M 頂点X-28,頂点Y A 28,28 0 0 0 頂点X,頂点Y+28' fill='none' stroke='#dc2626' stroke-width='2.5'/>
<text x='頂点X-22' y='頂点Y+26' font-size='16' font-weight='bold' fill='#dc2626'>x</text>
※ 求める角には必ず赤いアークと「x」ラベルを付ける。問題文は「∠DBP」などの記号だけでなく「図の角x」と書く。

■ 辺の途中の直角マーク（∧形）:
<path d='M 点X-10,底辺Y L 点X,底辺Y-14 L 点X+10,底辺Y' fill='none' stroke='#333' stroke-width='1.5'/>

■ 斜線塗り（斜線部分の面積を求める問題）:
<defs><pattern id='h' patternUnits='userSpaceOnUse' width='8' height='8' patternTransform='rotate(45)'><line x1='0' y1='0' x2='0' y2='8' stroke='#1d4ed8' stroke-width='1.5'/></pattern></defs>
<polygon points='...' fill='url(#h)' stroke='#1d4ed8' stroke-width='1.5'/>

【SVGテンプレート集】

■ 長方形（頂点ラベル・直角マーク付き）:
<svg viewBox='0 0 320 220' xmlns='http://www.w3.org/2000/svg'><rect x='60' y='40' width='200' height='140' fill='#dbeafe' stroke='#1d4ed8' stroke-width='2'/><rect x='60' y='166' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/><rect x='246' y='166' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/><text x='48' y='35' font-size='14' font-weight='bold' fill='#333'>A</text><text x='263' y='35' font-size='14' font-weight='bold' fill='#333'>B</text><text x='263' y='200' font-size='14' font-weight='bold' fill='#333'>C</text><text x='48' y='200' font-size='14' font-weight='bold' fill='#333'>D</text><text x='160' y='28' text-anchor='middle' font-size='13' fill='#333'>8cm</text><text x='275' y='115' font-size='13' fill='#333'>5cm</text></svg>

■ 直角三角形（全辺点線・頂点ラベル付き）:
<svg viewBox='0 0 320 230' xmlns='http://www.w3.org/2000/svg'><polygon points='160,25 50,200 270,200' fill='#dbeafe' stroke='#333' stroke-width='1.5' stroke-dasharray='6,4'/><path d='M 150,37 L 160,25 L 170,37' fill='none' stroke='#333' stroke-width='1.5'/><text x='152' y='15' font-size='14' font-weight='bold' fill='#333'>A</text><text x='32' y='215' font-size='14' font-weight='bold' fill='#333'>B</text><text x='272' y='215' font-size='14' font-weight='bold' fill='#333'>C</text><text x='88' y='120' font-size='13' fill='#333'>6cm</text><text x='215' y='120' font-size='13' fill='#333'>8cm</text><text x='160' y='220' text-anchor='middle' font-size='13' fill='#333'>10cm</text></svg>

■ 台形（辺上に点Pあり・内部線付き）:
<svg viewBox='0 0 320 230' xmlns='http://www.w3.org/2000/svg'><polygon points='80,30 130,30 280,200 50,200' fill='#dbeafe' stroke='#333' stroke-width='1.5' stroke-dasharray='6,4'/><rect x='50' y='186' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/><rect x='80' y='30' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/><circle cx='220' cy='130' r='3' fill='#333'/><line x1='80' y1='30' x2='220' y2='130' stroke='#333' stroke-width='1.5'/><line x1='50' y1='200' x2='220' y2='130' stroke='#333' stroke-width='1.5'/><text x='62' y='22' font-size='14' font-weight='bold' fill='#333'>A</text><text x='132' y='22' font-size='14' font-weight='bold' fill='#333'>D</text><text x='32' y='215' font-size='14' font-weight='bold' fill='#333'>B</text><text x='282' y='215' font-size='14' font-weight='bold' fill='#333'>C</text><text x='225' y='125' font-size='14' font-weight='bold' fill='#333'>P</text><text x='100' y='22' text-anchor='middle' font-size='13' fill='#333'>2cm</text><text x='35' y='118' font-size='13' fill='#333'>8cm</text><text x='165' y='215' text-anchor='middle' font-size='13' fill='#333'>8cm</text></svg>

■ 正方形＋各辺中点＋対角線＋斜線部分:
<svg viewBox='0 0 280 280' xmlns='http://www.w3.org/2000/svg'><defs><pattern id='h' patternUnits='userSpaceOnUse' width='8' height='8' patternTransform='rotate(45)'><line x1='0' y1='0' x2='0' y2='8' stroke='#1d4ed8' stroke-width='1.5'/></pattern></defs><rect x='40' y='40' width='200' height='200' fill='none' stroke='#333' stroke-width='2'/><circle cx='40' cy='140' r='3' fill='#333'/><circle cx='140' cy='240' r='3' fill='#333'/><circle cx='240' cy='140' r='3' fill='#333'/><circle cx='140' cy='40' r='3' fill='#333'/><line x1='40' y1='40' x2='140' y2='240' stroke='#333' stroke-width='1.5'/><line x1='40' y1='240' x2='240' y2='140' stroke='#333' stroke-width='1.5'/><line x1='240' y1='40' x2='40' y2='140' stroke='#333' stroke-width='1.5'/><line x1='240' y1='240' x2='140' y2='40' stroke='#333' stroke-width='1.5'/><polygon points='140,40 240,140 140,240 40,140' fill='url(#h)' stroke='#1d4ed8' stroke-width='1.5'/><text x='25' y='35' font-size='14' font-weight='bold'>A</text><text x='243' y='35' font-size='14' font-weight='bold'>D</text><text x='25' y='252' font-size='14' font-weight='bold'>B</text><text x='243' y='252' font-size='14' font-weight='bold'>C</text><text x='22' y='144' font-size='13' font-weight='bold'>P</text><text x='136' y='258' font-size='13' font-weight='bold'>Q</text><text x='244' y='144' font-size='13' font-weight='bold'>R</text><text x='136' y='32' font-size='13' font-weight='bold'>S</text></svg>

■ 長方形内に辺の途中から複数の線（角度問題）:
<svg viewBox='0 0 320 220' xmlns='http://www.w3.org/2000/svg'><line x1='40' y1='35' x2='280' y2='35' stroke='#94a3b8' stroke-width='1.5' stroke-dasharray='6,4'/><line x1='40' y1='35' x2='40' y2='185' stroke='#94a3b8' stroke-width='1.5' stroke-dasharray='6,4'/><line x1='280' y1='35' x2='280' y2='185' stroke='#1d4ed8' stroke-width='2'/><line x1='40' y1='185' x2='280' y2='185' stroke='#1d4ed8' stroke-width='2'/><rect x='40' y='168' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/><rect x='266' y='168' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/><rect x='266' y='35' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/><circle cx='40' cy='120' r='3' fill='#333'/><line x1='40' y1='120' x2='160' y2='185' stroke='#333' stroke-width='2'/><line x1='40' y1='120' x2='280' y2='35' stroke='#333' stroke-width='2'/><line x1='160' y1='185' x2='280' y2='35' stroke='#333' stroke-width='2'/><path d='M 150,182 L 158,170 L 168,182' fill='none' stroke='#333' stroke-width='1.5'/><path d='M 65,120 A 25,25 0 0 0 53,100' fill='none' stroke='#333' stroke-width='1.5'/><text x='57' y='117' font-size='12' fill='#333'>66°</text><path d='M 257,57 A 28,28 0 0 0 270,72' fill='none' stroke='#dc2626' stroke-width='2.5'/><text x='243' y='75' font-size='16' font-weight='bold' fill='#dc2626'>x</text></svg>

■ 長方形＋辺上2点から頂点への線（角度問題・上下左右の点から対角への線）:
<svg viewBox='0 0 320 230' xmlns='http://www.w3.org/2000/svg'><line x1='40' y1='30' x2='280' y2='30' stroke='#94a3b8' stroke-width='1.5' stroke-dasharray='6,4'/><line x1='40' y1='30' x2='40' y2='195' stroke='#94a3b8' stroke-width='1.5' stroke-dasharray='6,4'/><line x1='280' y1='30' x2='280' y2='195' stroke='#333' stroke-width='2'/><line x1='40' y1='195' x2='280' y2='195' stroke='#333' stroke-width='2'/><rect x='40' y='30' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/><rect x='266' y='30' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/><rect x='266' y='181' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/><rect x='40' y='181' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/><circle cx='40' cy='110' r='3' fill='#333'/><circle cx='140' cy='195' r='3' fill='#333'/><line x1='40' y1='110' x2='280' y2='30' stroke='#333' stroke-width='2'/><line x1='140' y1='195' x2='280' y2='30' stroke='#333' stroke-width='2'/><line x1='40' y1='110' x2='140' y2='195' stroke='#333' stroke-width='2'/><path d='M 130,195 L 140,178 L 150,195' fill='none' stroke='#333' stroke-width='1.5'/><path d='M 40,135 A 25,25 0 0 1 59,126' fill='none' stroke='#333' stroke-width='1.5'/><text x='48' y='150' font-size='12' fill='#333'>66°</text><path d='M 266,50 A 28,28 0 0 1 256,38' fill='none' stroke='#dc2626' stroke-width='2.5'/><text x='240' y='60' font-size='16' font-weight='bold' fill='#dc2626'>x</text><text x='25' y='24' font-size='14' font-weight='bold' fill='#333'>A</text><text x='283' y='24' font-size='14' font-weight='bold' fill='#333'>B</text><text x='283' y='212' font-size='14' font-weight='bold' fill='#333'>C</text><text x='25' y='212' font-size='14' font-weight='bold' fill='#333'>D</text><text x='20' y='114' font-size='14' font-weight='bold' fill='#333'>P</text><text x='134' y='213' font-size='14' font-weight='bold' fill='#333'>Q</text></svg>

■ L字型（折れた四角形）:
<svg viewBox='0 0 320 240' xmlns='http://www.w3.org/2000/svg'><polygon points='40,200 40,40 200,40 200,120 280,120 280,200' fill='#dbeafe' stroke='#1d4ed8' stroke-width='2'/><line x1='40' y1='120' x2='200' y2='120' stroke='#94a3b8' stroke-width='1.5' stroke-dasharray='5,3'/><line x1='200' y1='40' x2='200' y2='200' stroke='#94a3b8' stroke-width='1.5' stroke-dasharray='5,3'/><text x='120' y='28' text-anchor='middle' font-size='13' fill='#333'>8cm</text><text x='18' y='85' font-size='13' fill='#333'>5cm</text><text x='18' y='165' font-size='13' fill='#333'>4cm</text><text x='240' y='115' text-anchor='middle' font-size='13' fill='#333'>4cm</text><text x='292' y='165' font-size='13' fill='#333'>4cm</text></svg>

■ 円・おうぎ形:
<svg viewBox='0 0 300 220' xmlns='http://www.w3.org/2000/svg'><circle cx='150' cy='110' r='80' fill='#dbeafe' stroke='#1d4ed8' stroke-width='2'/><line x1='150' y1='110' x2='150' y2='30' stroke='#1d4ed8' stroke-width='1.5'/><line x1='150' y1='110' x2='230' y2='110' stroke='#1d4ed8' stroke-width='1.5'/><rect x='150' y='96' width='14' height='14' fill='none' stroke='#333' stroke-width='1.5'/><text x='195' y='100' font-size='13' fill='#333'>6cm</text></svg>

■ 速さ・距離の図:
<svg viewBox='0 0 320 110' xmlns='http://www.w3.org/2000/svg'><line x1='30' y1='55' x2='280' y2='55' stroke='#333' stroke-width='2'/><polygon points='280,50 292,55 280,60' fill='#333'/><circle cx='30' cy='55' r='5' fill='#1d4ed8'/><circle cx='280' cy='55' r='5' fill='#dc2626'/><text x='30' y='78' text-anchor='middle' font-size='13' fill='#1d4ed8'>A地点</text><text x='280' y='78' text-anchor='middle' font-size='13' fill='#dc2626'>B地点</text><text x='155' y='40' text-anchor='middle' font-size='14' fill='#333'>120km</text></svg>

■ 表（行き帰り・速さなど）:
<svg viewBox='0 0 320 160' xmlns='http://www.w3.org/2000/svg'><rect x='20' y='20' width='280' height='120' fill='none' stroke='#333' stroke-width='1.5'/><line x1='110' y1='20' x2='110' y2='140' stroke='#333' stroke-width='1.5'/><line x1='215' y1='20' x2='215' y2='140' stroke='#333' stroke-width='1.5'/><line x1='20' y1='60' x2='300' y2='60' stroke='#333' stroke-width='1.5'/><line x1='20' y1='100' x2='300' y2='100' stroke='#333' stroke-width='1.5'/><text x='65' y='46' text-anchor='middle' font-size='13'>　</text><text x='162' y='46' text-anchor='middle' font-size='13'>行き</text><text x='258' y='46' text-anchor='middle' font-size='13'>帰り</text><text x='65' y='86' text-anchor='middle' font-size='13'>速さ</text><text x='162' y='86' text-anchor='middle' font-size='13'>60km/h</text><text x='258' y='86' text-anchor='middle' font-size='13'>40km/h</text><text x='65' y='126' text-anchor='middle' font-size='13'>時間</text><text x='162' y='126' text-anchor='middle' font-size='13'>□時間</text><text x='258' y='126' text-anchor='middle' font-size='13'>□時間</text></svg>

【problemフィールドの書き方】
- 何を求めるかを問題文に必ず明示する。「大きさを求めなさい」だけでは不十分
- 角度を求める問題：「下の図で、角xは何度ですか。」
- 面積を求める問題：「下の図の斜線部分の面積を求めなさい。」または「色のついた部分の面積は何cm²ですか。」
- 辺の長さを求める問題：「下の図でxの長さは何cmですか。」
- 速さを求める問題：「〇〇の速さは時速何kmですか。」
- 時間を求める問題：「〇〇にかかる時間は何分ですか。」
- 体積・容積を求める問題：「この立体の体積は何cm³ですか。」
- 図がある場合：「∠DBP」などの記号だけで問わず、図中の「x」ラベルと対応させる

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


def search_similar_problems(subject: str, problem_text: str) -> str:
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key or not problem_text.strip():
        return ""
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=api_key)
        if subject == "算数":
            query = f"中学受験 小学生 算数 {problem_text[:60]} 問題 解き方"
        else:
            query = f"中学受験 小学生 国語 {problem_text[:60]} 問題"
        results = client.search(query=query, max_results=3, search_depth="basic")
        snippets = []
        for r in results.get("results", []):
            content = r.get("content", "").strip()
            if content:
                snippets.append(f"・「{r.get('title', '')}」より:\n{content[:400]}")
        return "\n\n".join(snippets)
    except Exception:
        return ""


def build_text_prompt(subject: str, problem: str, search_context: str = "") -> str:
    if subject == "算数":
        base = f"あなたは中学受験専門の算数の先生です。\n以下の間違えた問題と同じ種類・難易度の類似問題を3問作ってください。\n元の間違えた問題: {problem}\n\n{MATH_NOTES}"
    else:
        base = f"あなたは中学受験専門の国語の先生です。\n以下の間違えた問題と同じ種類・難易度の類似問題を3問作ってください。\n元の間違えた問題: {problem}\n\n{JAPANESE_NOTES}"
    if search_context:
        base += f"\n\n【参考：Web上の類似問題情報】\n（数値・設定・登場人物は変えてオリジナルの問題を作成すること。著作権に注意し文章をそのままコピーしないこと）\n{search_context}"
    return base + f"\n\n以下のJSON形式のみで返してください（他のテキスト不要）:\n{_json_format(subject)}"


def build_image_prompt(subject: str, search_context: str = "") -> str:
    if subject == "算数":
        base = f"あなたは中学受験専門の算数の先生です。\n画像に写っている問題を読み取り、同じ種類・難易度の類似問題を3問作ってください。\n\n{MATH_NOTES}"
    else:
        base = f"あなたは中学受験専門の国語の先生です。\n画像に写っている問題を読み取り、同じ種類・難易度の類似問題を3問作ってください。\n\n{JAPANESE_NOTES}"
    if search_context:
        base += f"\n\n【参考：Web上の類似問題情報】\n（数値・設定・登場人物は変えてオリジナルの問題を作成すること。著作権に注意し文章をそのままコピーしないこと）\n{search_context}"
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
            search_context = ""  # 画像入力時はテキストがないため検索スキップ
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4000,
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
                        {"type": "text", "text": build_image_prompt(subject, search_context)}
                    ]
                }]
            )
            original_label = "（画像で入力された問題）"
        else:
            search_context = search_similar_problems(subject, problem.strip())
            if search_context:
                print(f"[Tavily] Web検索成功: {len(search_context)}文字の参考情報を取得")
            else:
                print("[Tavily] Web検索スキップ（APIキー未設定 or 結果なし）")
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4000,
                messages=[{"role": "user", "content": build_text_prompt(subject, problem.strip(), search_context)}]
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
            "problem_id": problem_id,
            "search_used": bool(search_context)
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
