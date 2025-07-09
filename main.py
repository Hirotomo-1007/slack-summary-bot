# main.py --------------------------------------------------------
import os, datetime, time, textwrap, pytz
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from groq import Groq

# ---- 環境変数 ------------------------------------------------
BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
GROQ_TOKEN = os.environ["GROQ_API_KEY"]
HUMAN_UID = "UJJ7FT620"

client = WebClient(token=BOT_TOKEN)

# ---- 要約関数 ------------------------------------------------
def summarize(text: str) -> str:
    g = Groq(api_key=GROQ_TOKEN)
    prompt = textwrap.dedent(f"""\
        以下は Slack の会話ログです。重要な決定事項・依頼・次アクションだけ
        箇条書き 3〜5 行で日本語で要約してください。
        ===
        {text}
        ===
    """)
    res = g.chat.completions.create(
        model="llama3-70b-8192",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
    )
    return res.choices[0].message.content.strip()

# ---- APIラッパー (レート制限対応) ----------------------------
def safe_api(func, *args, **kwargs):
    while True:
        try:
            return func(*args, **kwargs)
        except SlackApiError as e:
            if e.response["error"] == "ratelimited":
                wait = int(e.response.headers.get("Retry-After", "20"))
                print(f"Rate-limited… {wait}s 待機")
                time.sleep(wait + 1)
            else:
                raise

# ---- 参加チャンネル取得 -------------------------------------
def fetch_my_channels(uid: str):
    res = safe_api(
        client.users_conversations,
        types="public_channel,private_channel,im,mpim",
        limit=1000,
    )
    return [c["id"] for c in res["channels"]]

# ---- メッセージ＋スレッド取得 -------------------------------
def fetch_today_msgs_with_threads(ch_id: str, oldest_ts: float):
    msgs, cursor = [], None
    while True:
        res = safe_api(
            client.conversations_history,
            channel=ch_id,
            oldest=oldest_ts,
            cursor=cursor,
            limit=200,
        )
        for m in res["messages"]:
            if "text" not in m:
                continue
            msgs.append(m["text"])
            # スレッドがあれば取得
            if "thread_ts" in m and m["ts"] == m["thread_ts"]:
                replies = safe_api(
                    client.conversations_replies,
                    channel=ch_id,
                    ts=m["thread_ts"]
                ).get("messages", [])[1:]  # 先頭は親なので除く
                for r in replies:
                    if "text" in r:
                        msgs.append(r["text"])

        cursor = res.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(1)
    return msgs

# ---- メイン処理 ---------------------------------------------
def run_daily_summary():
    tz = pytz.timezone("Asia/Tokyo")
    start_ts = datetime.datetime.now(tz).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()

    bot_uid = safe_api(client.auth_test)["user_id"]
    channel_ids = fetch_my_channels(bot_uid)

    report = []
    for cid in channel_ids:
        info = safe_api(client.conversations_info, channel=cid)["channel"]
        name = info.get("name") or f"<{cid}>"

        messages = fetch_today_msgs_with_threads(cid, start_ts)

        if not messages:
            summary = "（対象メッセージなし）"
        else:
            joined = "\n".join(messages)
            summary = summarize(joined)

        report.append(f"▶︎ #{name}\n{summary}\n")
        time.sleep(1)

    full_text = f"*📣 今日のまとめ（{datetime.date.today()}）*\n\n" + "\n".join(report)
    dm_chan = safe_api(client.conversations_open, users=HUMAN_UID)["channel"]["id"]
    safe_api(client.chat_postMessage, channel=dm_chan, text=full_text)

    print("✓ まとめ送信完了")

# ---- 実行エントリポイント -----------------------------------
if __name__ == "__main__":
    try:
        run_daily_summary()
    except SlackApiError as e:
        print("Slack API Error:", e)
