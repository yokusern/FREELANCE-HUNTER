#!/bin/bash
# FREELANCE-HUNTER ローカルセットアップ
# 実行: bash setup_local.sh

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== FREELANCE-HUNTER セットアップ ==="
echo ""

# Python 仮想環境
if [ ! -d "$DIR/.venv" ]; then
  echo "→ 仮想環境を作成中..."
  python3 -m venv "$DIR/.venv"
fi

echo "→ 依存ライブラリをインストール中..."
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

# .env 作成
if [ ! -f "$DIR/.env" ]; then
  echo ""
  echo "=== 環境変数の設定 ==="
  read -p "Discord Webhook URL (DISCORD_WEBHOOK_FREELANCE): " DISCORD
  read -p "Gemini API Key: " GEMINI
  cat > "$DIR/.env" <<EOF
DISCORD_WEBHOOK_FREELANCE=$DISCORD
GEMINI_API_KEY=$GEMINI
EOF
  echo "→ .env を作成しました"
fi

# 実行スクリプト
cat > "$DIR/run.sh" <<EOF
#!/bin/bash
set -a; source "$(dirname "\$0")/.env"; set +a
"$(dirname "\$0")/.venv/bin/python" "$(dirname "\$0")/hunter.py" >> "$(dirname "\$0")/hunter.log" 2>&1
EOF
chmod +x "$DIR/run.sh"

# launchd plist（macOS 自動起動）
PLIST="$HOME/Library/LaunchAgents/com.freelance-hunter.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.freelance-hunter</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DIR/run.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$DIR/hunter.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/hunter.log</string>
</dict>
</plist>
EOF

# launchd に登録
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo ""
echo "=== セットアップ完了 ==="
echo "毎時間自動で案件スキャンします。"
echo "ログ: $DIR/hunter.log"
echo ""
echo "手動テスト実行:"
echo "  bash $DIR/run.sh"
