import os
import re
import json
import time
import imaplib
import email as email_lib
import random
import smtplib
import requests
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import make_msgid
from bs4 import BeautifulSoup
from supabase import create_client, Client
from openai import OpenAI

# ==========================================
# CONFIGURATION (LOADED FROM GITHUB SECRETS)
# ==========================================
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

# 100% Free OpenRouter / Groq LLM for extracting Owner Name, Address & Formatting
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

DAILY_SEND_LIMIT = 20          # Safe daily limit per run (2 runs/day = 40 emails/day)
FOLLOWUP_DELAY_HOURS = 60      # 2.5 days (60 hours)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
llm_client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL) if LLM_API_KEY else None

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
]

# Dynamic Search Pool so every GitHub Action run scrapes fresh markets
NICHES = ["plumbing company", "HVAC repair", "emergency plumber", "roofing contractor", "dental clinic", "auto repair shop"]
CITIES = [
    "Austin TX", "Dallas TX", "Houston TX", "Denver CO", "Phoenix AZ",
    "Tampa FL", "Orlando FL", "Atlanta GA", "Charlotte NC", "Nashville TN",
    "Columbus OH", "Indianapolis IN", "Seattle WA", "San Diego CA", "Las Vegas NV"
]

# ==========================================
# 1. IMAP REPLY CHECKER (AUTO-STOPS FOLLOWUPS)
# ==========================================
def sync_gmail_replies():
    """Scans recent Gmail inbox messages and marks replied leads in Supabase."""
    print("📥 Checking Gmail Inbox for prospect replies...")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        # Search emails from the last 7 days
        since_date = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(SINCE "{since_date}")')
        if status != "OK" or not messages[0]:
            mail.logout()
            return

        replied_emails = set()
        for num in messages[0].split()[-100:]:  # Check last 100 emails
            _, data = mail.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            if data and data[0]:
                msg = email_lib.message_from_bytes(data[0][1])
                from_header = msg.get("From", "")
                match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', from_header)
                if match:
                    sender = match.group(0).lower()
                    if sender != GMAIL_USER.lower():
                        replied_emails.add(sender)

        mail.logout()

        if replied_emails:
            # Update any matching leads in Supabase that are awaiting follow-up
            res = supabase.table("leads").select("id, email").in_("email", list(replied_emails)).eq("status", "initial_sent").execute()
            for lead in (res.data or []):
                supabase.table("leads").update({"status": "replied"}).eq("id", lead["id"]).execute()
                print(f"🎉 Reply detected! Marked {lead['email']} as 'replied' (follow-up cancelled).")
    except Exception as e:
        print(f"[IMAP Warning] Could not check replies: {e}")

# ==========================================
# 2. SCRAPER & STRUCTURED DATA EXTRACTOR
# ==========================================
def get_headers():
    return {"User-Agent": random.choice(USER_AGENTS)}

def extract_emails(text: str) -> list[str]:
    pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    found = re.findall(pattern, text)
    blacklist = (
        'sentry', 'wixpress', 'example.com', '.png', '.jpg', '.jpeg', '.webp', '.svg',
        'noreply', 'no-reply', 'support@google', 'privacy@', 'abuse@', 'yourdomain', 'email.com'
    )
    valid = []
    for e in set(found):
        e_low = e.lower().strip(".")
        if not any(b in e_low for b in blacklist) and len(e_low) < 65:
            valid.append(e_low)
    return valid

def scrape_website_details(url: str, fallback_name: str, city_hint: str) -> dict | None:
    try:
        resp = requests.get(url, headers=get_headers(), timeout=10)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        combined_text = soup.get_text(" ", strip=True)
        emails = extract_emails(resp.text)

        # Crawl /contact or /about pages if email or owner info isn't obvious
        subpages_checked = 0
        for link in soup.find_all("a", href=True):
            if subpages_checked >= 2:
                break
            href = link["href"].lower()
            if any(k in href for k in ["contact", "about", "team", "our-story"]):
                full_url = href if href.startswith("http") else requests.compat.urljoin(url, link["href"])
                if url.split("//")[-1].split("/")[0] not in full_url:
                    continue
                try:
                    sub_resp = requests.get(full_url, headers=get_headers(), timeout=8)
                    if sub_resp.status_code == 200:
                        subpages_checked += 1
                        sub_emails = extract_emails(sub_resp.text)
                        emails = list(set(emails + sub_emails))
                        combined_text += " " + BeautifulSoup(sub_resp.text, "html.parser").get_text(" ", strip=True)
                except Exception:
                    continue

        if not emails:
            return None

        # Prioritize direct/owner/info emails over generic ones
        emails.sort(key=lambda x: (0 if not x.startswith(("info@", "contact@", "office@")) else 1))
        chosen_email = emails[0]

        owner_name = None
        business_name = fallback_name
        business_address = city_hint

        # Extract clean Business Name, Physical Address, and Owner Name via Free LLM
        if llm_client:
            prompt = f"""Extract exact business info from this website text. Return ONLY valid JSON with keys:
"owner_name": string (First + Last name of founder/owner/president, or null if not explicitly mentioned),
"business_name": string (Clean official business name),
"business_address": string (Full street address, city, state, zip if present, otherwise "{city_hint}").

Text:
{combined_text[:3000]}"""
            try:
                comp = llm_client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                )
                raw = re.sub(r"^```json|```$", "", comp.choices[0].message.content.strip()).strip()
                data = json.loads(raw)
                owner_name = data.get("owner_name")
                if owner_name and str(owner_name).lower() in ("null", "none", "unknown", "n/a"):
                    owner_name = None
                business_name = data.get("business_name") or fallback_name
                business_address = data.get("business_address") or city_hint
            except Exception:
                pass

        return {
            "email": chosen_email,
            "owner_name": owner_name,
            "business_name": business_name,
            "business_address": business_address,
            "website": url,
            "status": "pending"
        }
    except Exception:
        return None

def run_scraper(niche: str, city: str, max_results: int = 12):
    query = f"{niche} {city} contact email"
    print(f"🔍 Scraping: '{query}'")
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers=get_headers(),
            timeout=15
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        results = soup.select(".result")[:max_results]
    except Exception as e:
        print(f"[Search Error] {e}")
        return

    skip_domains = (
        "yelp.com", "yellowpages.com", "facebook.com", "instagram.com", "linkedin.com",
        "angi.com", "bbb.org", "thumbtack.com", "homeadvisor.com", "mapquest.com", "nextdoor.com"
    )

    saved = 0
    for res in results:
        a_tag = res.select_one(".result__a")
        if not a_tag:
            continue

        href = a_tag.get("href", "")
        if not href.startswith("http") or any(d in href.lower() for d in skip_domains):
            continue

        raw_title = a_tag.get_text(strip=True)
        clean_biz_name = re.split(r'\s*[-|–—:]\s*', raw_title)[0].strip()

        details = scrape_website_details(href, fallback_name=clean_biz_name, city_hint=city)
        if details:
            details["niche"] = niche
            try:
                # Upsert with ignore_duplicates=True guarantees 100% deduplication
                db_res = supabase.table("leads").upsert(
                    details, on_conflict="email", ignore_duplicates=True
                ).execute()
                if db_res.data:
                    saved += 1
                    print(f"  ✅ Saved: {details['business_name']} | {details['email']} | Owner: {details['owner_name']} | Addr: {details['business_address']}")
            except Exception as e:
                print(f"  [DB Skip/Error] {e}")

        time.sleep(random.uniform(1.5, 3.0))

    print(f"📦 New unique leads added for {city}: {saved}\n")

# ==========================================
# 3. EMAIL FORMATTER & THREADED GMAIL SENDER
# ==========================================
def send_gmail_threaded(to_email: str, subject: str, body_text: str, in_reply_to: str = None) -> str | None:
    """Sends email via Gmail SMTP and returns the Message-ID for future threading."""
    msg = MIMEMultipart()
    msg["From"] = f"Rohan Singh <{GMAIL_USER}>"
    msg["To"] = to_email
    msg["Subject"] = subject

    new_msg_id = make_msgid(domain="gmail.com")
    msg["Message-ID"] = new_msg_id

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    msg.attach(MIMEText(body_text, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        return new_msg_id
    except Exception as e:
        print(f"[SMTP Error] {to_email}: {e}")
        return None

def build_initial_email(lead: dict) -> tuple[str, str]:
    owner = lead.get("owner_name")
    biz = lead["business_name"]
    addr = lead.get("business_address")

    greeting = f"Hi {owner.split()[0]}," if owner else f"Hi {biz} team,"
    addr_mention = f" at {addr}" if addr and len(addr) > 12 else ""

    subject = f"Quick question for {biz}"
    body = f"""{greeting}

I came across {biz}{addr_mention} while researching top local service providers in your area.

When technicians are out on jobs or calls come in after hours, even 2–3 missed calls a week can mean thousands in lost jobs going straight to the next company on Google.

At PixelRoot, we build 24/7 AI voice receptionists trained specifically on your services and pricing so every single call gets answered, qualified, and booked straight onto your calendar automatically.

Would you be open to me sending over a 60-second custom demo call tailored to {biz}?

Best,
Rohan Singh
Founder, PixelRoot
"""
    return subject, body

def build_followup_email(lead: dict) -> tuple[str, str]:
    owner = lead.get("owner_name")
    biz = lead["business_name"]
    greeting = f"Hi {owner.split()[0]}," if owner else f"Hi {biz} team,"

    subject = f"Re: Quick question for {biz}"
    body = f"""{greeting}

Just floating this back to the top of your inbox in case it got buried during a busy week.

No pitch deck or long meeting needed—I can build a quick test number for {biz} so you can call it yourself and hear how it handles customer bookings.

Want me to spin that up for you?

Best,
Rohan Singh
Founder, PixelRoot
"""
    return subject, body

# ==========================================
# 4. PIPELINE EXECUTION
# ==========================================
def send_due_followups(max_sends: int) -> int:
    print("🔄 Checking for leads ready for 2.5-day follow-up...")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=FOLLOWUP_DELAY_HOURS)).isoformat()

    res = (
        supabase.table("leads")
        .select("*")
        .eq("status", "initial_sent")
        .lte("initial_sent_at", cutoff)
        .limit(max_sends)
        .execute()
    )

    sent_count = 0
    for lead in (res.data or []):
        subject, body = build_followup_email(lead)
        sent_id = send_gmail_threaded(
            to_email=lead["email"],
            subject=subject,
            body_text=body,
            in_reply_to=lead.get("message_id")
        )
        if sent_id:
            supabase.table("leads").update({
                "status": "followup_sent",
                "followup_sent_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", lead["id"]).execute()
            sent_count += 1
            print(f"  🔁 Follow-up sent to {lead['email']} ({lead['business_name']})")
            time.sleep(random.uniform(15, 30))

    print(f"🎯 Total follow-ups sent: {sent_count}\n")
    return sent_count

def send_initial_outreach(max_sends: int) -> int:
    print("🚀 Sending initial emails to new pending leads...")
    res = supabase.table("leads").select("*").eq("status", "pending").limit(max_sends).execute()

    sent_count = 0
    for lead in (res.data or []):
        subject, body = build_initial_email(lead)
        msg_id = send_gmail_threaded(to_email=lead["email"], subject=subject, body_text=body)
        if msg_id:
            supabase.table("leads").update({
                "status": "initial_sent",
                "message_id": msg_id,
                "initial_sent_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", lead["id"]).execute()
            sent_count += 1
            print(f"  ✉️ Initial email sent to {lead['email']} ({lead['business_name']})")
            time.sleep(random.uniform(15, 30))

    print(f"🎯 Total initial emails sent: {sent_count}\n")
    return sent_count

if __name__ == "__main__":
    print("=== GOD-TIER OUTREACH ENGINE STARTED ===")

    # 1. Check Gmail Inbox first so we never follow up with people who replied
    sync_gmail_replies()

    # 2. Pick 3 random Niche + City combinations to scrape fresh leads every run
    for _ in range(3):
        run_scraper(random.choice(NICHES), random.choice(CITIES), max_results=10)

    # 3. Prioritize 2.5-day follow-ups first
    followups = send_due_followups(max_sends=10)

    # 4. Use remaining quota for brand-new initial emails
    remaining = max(0, DAILY_SEND_LIMIT - followups)
    if remaining > 0:
        send_initial_outreach(max_sends=remaining)

    print("=== RUN COMPLETE ===")
