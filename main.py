import os, datetime, time, textwrap, pytz
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from groq import Groq

# ---- 環境変数 ------------------------------------------------
BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
GROQ_TOKEN = os.environ["GROQ_API_KEY"]
HUMAN_UID = "UJJ7FT620"

# ←この行を追加（環境変数が無ければ既定で70Bを使う）
MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

client = WebClient(token=BOT_TOKEN)

# ---- 要約関数 ------------------------------------------------
def summarize(text: str) -> str:
    g = Groq(api_key=GROQ_TOKEN)
    prompt = textwrap.dedent(f"""
        以下は Slack の会話ログです。重要な決定事項・依頼・次アクションだけ
        箇条書き 3〜5 行で日本語で要約してください。
        ===
        {text}
        ===
    """)
    res = g.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
    )
    return res.choices[0].message.content.strip()

# ---- テキスト分割要約関数 -------------------------------------
def summarize_in_chunks(text: str, max_chars=6000) -> list:
    chunks = [text[i:i + max_chars] for i in range(0, len(text), max_chars)]
    summaries = [summarize(chunk) for chunk in chunks]
    return summaries

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

# ---- メッセージ＋スレッド取得（時間範囲指定） ------------------
def fetch_msgs_with_threads(ch_id: str, oldest_ts: float, latest_ts: float):
    msgs, cursor = [], None
    while True:
        res = safe_api(
            client.conversations_history,
            channel=ch_id,
            oldest=oldest_ts,
            latest=latest_ts,
            inclusive=True,
            cursor=cursor,
            limit=200,
        )
        for m in res["messages"]:
            if "text" not in m:
                continue
            msgs.append(m["text"])
            if "thread_ts" in m and m["ts"] == m["thread_ts"]:
                replies = safe_api(
                    client.conversations_replies,
                    channel=ch_id,
                    ts=m["thread_ts"]
                ).get("messages", [])[1:]
                for r in replies:
                    if "text" in r:
                        msgs.append(r["text"])

        cursor = res.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(1)
    return msgs

# ---- メイン処理 ---------------------------------------------
def run_daily_summary(start_hour: int, end_hour: int):
    tz = pytz.timezone("Asia/Tokyo")
    now = datetime.datetime.now(tz)

    start_dt = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end_dt = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)

    if start_hour > end_hour:
        start_dt -= datetime.timedelta(days=1)

    start_ts = start_dt.timestamp()
    end_ts = end_dt.timestamp()

    bot_uid = safe_api(client.auth_test)["user_id"]
    channel_ids = fetch_my_channels(bot_uid)

    report = []
    for cid in channel_ids:
        info = safe_api(client.conversations_info, channel=cid)["channel"]
        name = info.get("name") or f"<{cid}>"

        messages = fetch_msgs_with_threads(cid, start_ts, end_ts)

        if not messages:
            summaries = ["（対象メッセージなし）"]
        else:
            joined = "\n".join(messages)
            summaries = summarize_in_chunks(joined)

        for idx, summary in enumerate(summaries):
            header = f"\u25b6\ufe0e #{name} (Part {idx+1}/{len(summaries)})" if len(summaries) > 1 else f"\u25b6\ufe0e #{name}"
            report.append(f"{header}\n{summary}\n")
            time.sleep(1)

    range_text = f"{start_hour:02d}:00〜{end_hour:02d}:00"
    full_text = f"*\ud83d\udce3 Slackまとめ（{range_text}）[{now.date()}]*\n\n" + "\n".join(report)
    dm_chan = safe_api(client.conversations_open, users=HUMAN_UID)["channel"]["id"]
    safe_api(client.chat_postMessage, channel=dm_chan, text=full_text)

    print("\u2713 まとめ送信完了")

# ---- 実行エントリポイント -----------------------------------
if __name__ == "__main__":
    try:
        mode = os.environ.get("SUMMARY_MODE", "morning")  # "morning" or "afternoon"
        if mode == "afternoon":
            run_daily_summary(11, 19)  # 午後のまとめ
        else:
            run_daily_summary(19, 11)  # 夜〜午前のまとめ
    except SlackApiError as e:
        print("Slack API Error:", e)
