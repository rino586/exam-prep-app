# 受験対策アプリ 📚

小学6年生向けの中学受験対策Webアプリです。
間違えた問題を入力すると、AIが類似問題を3問自動生成します。

## 機能

- ✏️ 間違えた問題を入力（算数・国語）
- ✨ AIが類似問題を3問生成
- 💡 ヒント・答えを確認しながら練習
- 📋 過去の問題履歴から繰り返し練習

---

## 使い方（Render.comでネット公開）

### ステップ1: Anthropic APIキーを取得する

1. [https://console.anthropic.com](https://console.anthropic.com) にアクセス
2. アカウントを作成してログイン
3. 「API Keys」メニューから「Create Key」をクリック
4. 表示されたキー（`sk-ant-...`）をコピーして保存

### ステップ2: GitHubにアップロードする

1. [https://github.com](https://github.com) でアカウント作成
2. 「New repository」でリポジトリを作成（例: `exam-prep-app`）
3. このフォルダ内のファイルをすべてアップロード

### ステップ3: Render.comでデプロイする

1. [https://render.com](https://render.com) でアカウント作成
2. 「New +」→「Web Service」をクリック
3. GitHubリポジトリを選択
4. 以下の設定を確認（render.yamlが自動で読み込まれます）
5. 「Environment Variables」に以下を追加：
   - Key: `ANTHROPIC_API_KEY`
   - Value: ステップ1でコピーしたキー
6. 「Create Web Service」をクリック
7. 数分後にURLが表示されます。そのURLを別のPCで開けば使えます！

---

## ローカルで試す場合（開発者向け）

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-xxxx  # Windowsは set ANTHROPIC_API_KEY=...
python app.py
```

ブラウザで [http://localhost:8000](http://localhost:8000) を開く。
