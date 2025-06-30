import os
import smtplib
import feedparser
import json
from flask import Flask, request, render_template, redirect, url_for
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from openai import OpenAI

app = Flask(__name__)

SETTINGS_FILE = "settings.json"
SHEET_ID = "1yXACANpZs6aiGty1vDno5t6WX447yUJIC-hi4-tBDrQ"
MAX_ARTICLES = 10

def get_creds():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    return ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)

def get_auto_mode():
    with open(SETTINGS_FILE, "r") as f:
        return json.load(f).get("auto_mode", False)

def set_auto_mode(value: bool):
    with open(SETTINGS_FILE, "w") as f:
        json.dump({"auto_mode": value}, f)

def load_system_settings(sheet_id):
    creds = get_creds()
    client = gspread.authorize(creds)
    sheet = client.open_by_key(sheet_id).worksheet("Settings")
    rows = sheet.get_all_values()
    settings = {row[0].strip().lower(): row[1].strip() for row in rows if len(row) >= 2}
    return settings

def load_user_configs(sheet_id, run_time):
    creds = get_creds()
    client = gspread.authorize(creds)
    sheet = client.open_by_key(sheet_id).worksheet("Users")
    rows = sheet.get_all_values()[1:]
    configs = []
    for row in rows:
        if len(row) < 6:
            continue
        email = row[0]
        feeds = [url.strip() for url in row[1].split(";") if url.strip()]
        keywords = [kw.strip() for kw in row[2].split(",") if kw.strip()]
        active = row[3].strip().lower() == "true"
        schedule = row[4].strip().lower()
        name = row[5].strip()
        if active and (schedule == run_time or schedule == "båda"):
            configs.append({"email": email, "feeds": feeds, "keywords": keywords, "name": name})
    return configs

def fetch_articles(feeds, keywords):
    articles = []
    for feed in feeds:
        parsed_feed = feedparser.parse(feed)
        for entry in parsed_feed.entries:
            if any(keyword.lower() in entry.title.lower() for keyword in keywords):
                articles.append({"title": entry.title, "link": entry.link})
    return articles

def summarize_articles(articles):
    client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
    summaries = []
    with open("sent_links.txt", "a+") as sent:
        sent.seek(0)
        sent_links = set(line.strip() for line in sent.readlines())
    count = 0
    new_links = []
    for article in articles:
        if article['link'] in sent_links:
            continue
        if count >= MAX_ARTICLES:
            break
        try:
            prompt = f"Sammanfatta denna artikel kort och tydligt: {article['title']} ({article['link']})"
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}]
            )
            summary = response.choices[0].message.content.strip()
        except Exception as e:
            summary = f"Kunde inte sammanfatta: {str(e)}"
        summaries.append({"title": article['title'], "link": article['link'], "summary": summary})
        new_links.append(article['link'])
        count += 1
    with open("sent_links.txt", "a") as sent:
        for link in new_links:
            sent.write(link + "\n")
    return summaries

def send_email(summaries, recipient, name, settings):
    sender_email = os.environ['EMAIL_ADDRESS']
    sender_password = os.environ['EMAIL_PASSWORD']
    sender_name = settings.get("sender_name", "Omvärldskollen")
    subject_prefix = settings.get("subject_prefix", "Omvärldsbevakning")
    subject = f"{subject_prefix} - {datetime.now().strftime('%Y-%m-%d')}"

    msg = MIMEMultipart("alternative")
    msg['From'] = f"{sender_name} <{sender_email}>"
    msg['To'] = recipient
    msg['Subject'] = subject

    if summaries:
        overview_prompt = "Sammanfatta följande nyhetsrubriker med några meningar: " + ", ".join([s['title'] for s in summaries])
        try:
            client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": overview_prompt}]
            )
            overview = response.choices[0].message.content.strip()
        except Exception as e:
            overview = f"(Kunde inte skapa översikt: {str(e)})"

        html = f"""
        <html><body style='font-family:Arial,sans-serif;'>
        <h2>Hej {name} 👋</h2>
        <p>Här kommer dagens omvärldsbevakning:</p>
        <p><i>{overview}</i></p>
        <hr/>
        """
        for summary in summaries:
            html += f"""
            <div style='background:#f3f3f3;padding:10px;border-radius:10px;margin-bottom:10px;'>
            <h3>{summary['title']}</h3>
            <p>{summary['summary']}</p>
            <p><a href='{summary['link']}' target='_blank'>Läs hela artikeln →</a></p>
            </div>
            """
        html += "</body></html>"
    else:
        html = f"""
        <html><body style='font-family:Arial,sans-serif;'>
        <div style='text-align:center;'>
          <img src='https://lh4.googleusercontent.com/-V2_Uc4SrLNKn1xYhsxaemHN37QJtpnbQ-5Txu8JFQbrHntsGDpE7S_iq1p2_EWq6Cx3preGYCOFQ1Ees0_rJliTFXMtJZgisCS1yjy6Zrv9FiJhB6ydUtAgyqbtI1kU1RVgiiSSmXaeU06gFoGecw4Cu06H36k2e4mp_CkuJv-VQ0bWN-Glnw=w1280' alt='Omvärldskollen' style='max-width:100%; height:auto; margin-bottom:20px;'>
        </div>
        <h2>Hej {name} 👋</h2>
        <p>Du är uppdaterad! ✅</p>
        <p>Inga nya artiklar matchade dina bevakningsområden idag.</p>
        <p>Vi hör av oss när det dyker upp något nytt.</p>
        </body></html>
        """

    msg.attach(MIMEText(html, "html"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, recipient, msg.as_string())
        server.quit()
        print(f"✅ Mejl skickat till {recipient}.")
    except Exception as e:
        print(f"❌ Fel vid mejlutskick till {recipient}: {e}")

@app.context_processor
def inject_status():
    auto_mode = get_auto_mode()
    settings = load_system_settings(SHEET_ID)
    scheduled = settings.get("scheduled_hour", "-")
    manual = settings.get("manual_trigger", "true").lower()
    return dict(auto_mode=auto_mode, scheduled=scheduled, manual_allowed=(manual == "true"))

@app.route("/")
def home():
    return redirect(url_for("dashboard"))

@app.route("/dashboard")
def dashboard():
    creds = get_creds()
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).worksheet("Users")
    rows = sheet.get_all_values()
    headers = rows[0]
    data = rows[1:]

    users = []
    for row in data:
        user = {
            "Name": row[5] if len(row) > 5 else "",
            "Email": row[0] if len(row) > 0 else "",
            "Feeds": row[1] if len(row) > 1 else "",
            "Keywords": row[2] if len(row) > 2 else "",
            "Schedule": row[4] if len(row) > 4 else "",
            "Active": row[3] if len(row) > 3 else ""
        }
        users.append(user)

    return render_template("dashboard.html", users=users)

@app.route("/toggle")
def toggle():
    current = get_auto_mode()
    set_auto_mode(not current)
    return redirect(url_for("dashboard"))

@app.route("/run_user")
def run_user():
    email = request.args.get("email")
    if not email:
        return "❌ Ingen e-postadress angiven.", 400

    email = email.strip()

    creds = get_creds()
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).worksheet("Users")
    rows = sheet.get_all_values()
    headers = rows[0]
    data = rows[1:]

    for i, row in enumerate(data):
        if len(row) < 6:
            continue
        if row[0].strip().lower() == email.lower():
            feeds = [url.strip() for url in row[1].split(";") if url.strip()]
            keywords = [kw.strip() for kw in row[2].split(",") if kw.strip()]
            name = row[5].strip()
            settings = load_system_settings(SHEET_ID)
            articles = fetch_articles(feeds, keywords)
            summaries = summarize_articles(articles)
            send_email(summaries, email, name, settings)
            return f"ok"

    return f"❌ Hittade ingen användare med e-post: {email}", 404

@app.route("/edit_user", methods=["GET", "POST"])
def edit_user():
    email = request.args.get("email")
    if not email:
        return "❌ Ingen e-postadress angiven.", 400

    email = email.strip()

    creds = get_creds()
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).worksheet("Users")
    all_rows = sheet.get_all_values()
    headers = all_rows[0]
    rows = all_rows[1:]

    for i, row in enumerate(rows):
        if len(row) < 6:
            continue
        if row[0].strip().lower() == email.lower():
            if request.method == "POST":
                updated = [
                    request.form.get("email"),
                    request.form.get("feeds"),
                    request.form.get("keywords"),
                    "true" if request.form.get("active") else "false",
                    request.form.get("schedule"),
                    request.form.get("name"),
                ]
                sheet.update(f"A{i+2}:F{i+2}", [updated])
                return redirect("/dashboard")

            user = {
                "email": row[0],
                "feeds": row[1],
                "keywords": row[2],
                "active": row[3].lower() == "true",
                "schedule": row[4],
                "name": row[5]
            }
            return render_template("edit_user.html", user=user)

    return "❌ Användare hittades inte", 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
