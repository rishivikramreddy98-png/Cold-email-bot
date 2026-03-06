import os
import logging
import csv
import io
import threading
import re
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from sqlalchemy.orm import Session
from models import init_db, Lead, Template, Deal, Campaign, CampaignLog, User
from email_service import run_campaign
from reply_checker import ReplyChecker

# Load environment variables
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL", "sqlite:///bot_database.db")

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Database
SessionLocal = init_db(DB_URL)

# Conversation states
ADD_LEAD_EMAIL, ADD_LEAD_NAME, ADD_LEAD_COMPANY = range(3)
IMPORT_LEADS = 3
ADD_TEMPLATE_NAME, ADD_TEMPLATE_SUBJECT, ADD_TEMPLATE_BODY = range(4, 7)
ADD_DEAL_LEAD, ADD_DEAL_TITLE, ADD_DEAL_VALUE = range(7, 10)
START_CAMPAIGN_NAME, START_CAMPAIGN_TEMPLATE = range(10, 12)
SETUP_MAIL_EMAIL, SETUP_MAIL_PASSWORD = range(12, 14)

# --- Helpers ---
def get_or_create_user(db: Session, telegram_id: int):
    user = db.query(User).filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user

def detect_email_settings(email_address):
    domain = email_address.split('@')[-1]
    if "gmail.com" in domain:
        return {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "imap_host": "imap.gmail.com", "imap_port": 993}
    elif "outlook.com" in domain or "office365.com" in domain:
        return {"smtp_host": "smtp.office365.com", "smtp_port": 587, "imap_host": "outlook.office365.com", "imap_port": 993}
    else:
        return {"smtp_host": None, "smtp_port": None, "imap_host": None, "imap_port": None}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = SessionLocal()
    get_or_create_user(db, update.effective_user.id)
    db.close()
    welcome_text = (
        "🚀 *Welcome to Cold Email Outreach Bot!*\n\n"
        "Manage your leads, send cold emails, and track deals directly from Telegram.\n\n"
        "Use /help to see available commands."
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📋 *Available Commands:*\n\n"
        "👤 *Leads*\n"
        "/addlead - Add a lead manually\n"
        "/importleads - Import leads via CSV\n"
        "/viewleads - View all leads\n\n"
        "📧 *Templates*\n"
        "/templates - View email templates\n"
        "/addtemplate - Add a new template\n\n"
        "🚀 *Campaigns*\n"
        "/campaign - Create and start a campaign\n"
        "/stats - View campaign statistics\n\n"
        "💼 *Deals*\n"
        "/deals - View all deals\n"
        "/adddeal - Add a new deal\n\n"
        "⚙️ *System*\n"
        "/setupmail - Set up your sending email credentials\n"
        "/mymail - View your current email settings\n"
        "/enablereplies - Enable reply checking for your email\n"
        "/disablereplies - Disable reply checking for your email\n"
        "/help - Show this message\n"
        "/cancel - Cancel current operation"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

# --- User Email Setup Handlers ---
async def setup_mail_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please enter your email address (e.g., your@gmail.com):")
    return SETUP_MAIL_EMAIL

async def setup_mail_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email_address = update.message.text.strip()
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email_address):
        await update.message.reply_text("Invalid email format. Please try again or /cancel.")
        return SETUP_MAIL_EMAIL
    
    context.user_data['setup_email'] = email_address
    await update.message.reply_text("Please enter your email password or app-specific password:")
    return SETUP_MAIL_PASSWORD

async def setup_mail_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    email_address = context.user_data['setup_email']
    telegram_id = update.effective_user.id

    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)
    
    settings = detect_email_settings(email_address)
    if not settings['smtp_host']:
        await update.message.reply_text(
            "Could not auto-detect settings for your email provider. "
            "Please contact support with your email domain for manual setup, or use Gmail/Outlook."
        )
        db.close()
        return ConversationHandler.END

    user.smtp_email = email_address
    user.smtp_password = password
    user.smtp_host = settings['smtp_host']
    user.smtp_port = settings['smtp_port']
    user.imap_host = settings['imap_host']
    user.imap_port = settings['imap_port']
    
    try:
        db.commit()
        await update.message.reply_text(
            f"✅ Email settings saved for {email_address}. "
            f"SMTP: {user.smtp_host}:{user.smtp_port}, IMAP: {user.imap_host}:{user.imap_port}"
        )
    except Exception as e:
        db.rollback()
        await update.message.reply_text(f"❌ Error saving email settings: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

async def my_mail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)
    db.close()

    if user.smtp_email:
        status = "Enabled" if user.imap_enabled else "Disabled"
        text = (
            f"📧 *Your Email Settings:*\n\n"
            f"Email: `{user.smtp_email}`\n"
            f"SMTP Host: `{user.smtp_host}:{user.smtp_port}`\n"
            f"IMAP Host: `{user.imap_host}:{user.imap_port}`\n"
            f"Reply Checking: *{status}*\n"
            "_Note: Password is not displayed for security reasons._"
        )
    else:
        text = "You have not set up your email yet. Use /setupmail to configure it."
    await update.message.reply_text(text, parse_mode='Markdown')

async def enable_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)

    if not user.smtp_email or not user.smtp_password or not user.imap_host or not user.imap_port:
        await update.message.reply_text("Please set up your email credentials first using /setupmail.")
        db.close()
        return
    
    user.imap_enabled = True
    try:
        db.commit()
        await update.message.reply_text("✅ Reply checking enabled. You will be notified of replies from your leads.")
    except Exception as e:
        db.rollback()
        await update.message.reply_text(f"❌ Error enabling reply checking: {str(e)}")
    finally:
        db.close()

async def disable_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)
    
    user.imap_enabled = False
    try:
        db.commit()
        await update.message.reply_text("✅ Reply checking disabled.")
    except Exception as e:
        db.rollback()
        await update.message.reply_text(f"❌ Error disabling reply checking: {str(e)}")
    finally:
        db.close()

# --- Lead Handlers ---
async def add_lead_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter lead's email address:")
    return ADD_LEAD_EMAIL

async def add_lead_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['lead_email'] = update.message.text
    await update.message.reply_text("Enter lead's first name:")
    return ADD_LEAD_NAME

async def add_lead_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['lead_name'] = update.message.text
    await update.message.reply_text("Enter lead's company:")
    return ADD_LEAD_COMPANY

async def add_lead_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    company = update.message.text
    email = context.user_data['lead_email']
    name = context.user_data['lead_name']
    telegram_id = update.effective_user.id

    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)
    
    new_lead = Lead(user_id=user.id, email=email, first_name=name, company=company)
    db.add(new_lead)
    try:
        db.commit()
        await update.message.reply_text(f"✅ Lead {name} ({email}) added successfully!")
    except Exception as e:
        db.rollback()
        await update.message.reply_text(f"❌ Error adding lead: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

async def import_leads_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please upload a CSV file with columns: email, first_name, last_name, company")
    return IMPORT_LEADS

async def import_leads_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    content = await file.download_as_bytearray()
    telegram_id = update.effective_user.id

    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)

    try:
        csv_file = io.StringIO(content.decode('utf-8'))
        reader = csv.DictReader(csv_file)
        count = 0
        for row in reader:
            # Check if lead already exists for this user
            if not db.query(Lead).filter_by(user_id=user.id, email=row['email']).first():
                lead = Lead(user_id=user.id, email=row['email'], first_name=row.get('first_name'), 
                           last_name=row.get('last_name'), company=row.get('company'))
                db.add(lead)
                count += 1
        db.commit()
        await update.message.reply_text(f"✅ Successfully imported {count} new leads.")
    except Exception as e:
        db.rollback()
        await update.message.reply_text(f"❌ Error importing CSV: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

async def view_leads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)

    leads = db.query(Lead).filter_by(user_id=user.id).limit(15).all()
    if not leads:
        await update.message.reply_text("No leads found. Use /addlead or /importleads to add some.")
        db.close()
        return
    text = "👤 *Recent Leads:*\n\n" + "\n".join([f"• {l.first_name or 'N/A'} - {l.email} ({l.company or 'No Company'})" for l in leads])
    db.close()
    await update.message.reply_text(text, parse_mode='Markdown')

# --- Template Handlers ---
async def view_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)

    templates = db.query(Template).filter_by(user_id=user.id).all()
    if not templates:
        await update.message.reply_text("No templates found. Use /addtemplate to create one.")
        db.close()
        return
    text = "📧 *Templates:*\n\n" + "\n".join([f"ID: {t.id} | Name: {t.name} | Subject: {t.subject}" for t in templates])
    db.close()
    await update.message.reply_text(text, parse_mode='Markdown')

async def add_template_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter template name (e.g., 'Intro Email'):")
    return ADD_TEMPLATE_NAME

async def add_template_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['tmpl_name'] = update.message.text
    await update.message.reply_text("Enter email subject:")
    return ADD_TEMPLATE_SUBJECT

async def add_template_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['tmpl_subject'] = update.message.text
    await update.message.reply_text("Enter email body (use {first_name}, {company} as placeholders):")
    return ADD_TEMPLATE_BODY

async def add_template_body(update: Update, context: ContextTypes.DEFAULT_TYPE):
    body = update.message.text
    telegram_id = update.effective_user.id

    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)

    tmpl = Template(user_id=user.id, name=context.user_data['tmpl_name'], subject=context.user_data['tmpl_subject'], body=body)
    db.add(tmpl)
    try:
        db.commit()
        await update.message.reply_text("✅ Template saved successfully!")
    except Exception as e:
        db.rollback()
        await update.message.reply_text(f"❌ Error saving template: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

# --- Deal Handlers ---
async def view_deals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)

    deals = db.query(Deal).filter_by(user_id=user.id).all()
    if not deals:
        await update.message.reply_text("No deals found. Use /adddeal to create one.")
        db.close()
        return
    text = "💼 *Current Deals:*\n\n" + "\n".join([f"ID: {d.id} | {d.title} | ${d.value} | Stage: {d.stage}" for d in deals])
    db.close()
    await update.message.reply_text(text, parse_mode='Markdown')

async def add_deal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter Lead ID for this deal (view /viewleads):")
    return ADD_DEAL_LEAD

async def add_deal_lead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['deal_lead_id'] = update.message.text
    await update.message.reply_text("Enter deal title:")
    return ADD_DEAL_TITLE

async def add_deal_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['deal_title'] = update.message.text
    await update.message.reply_text("Enter deal value ($):")
    return ADD_DEAL_VALUE

async def add_deal_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)

    try:
        lead_id = int(context.user_data['deal_lead_id'])
        # Verify lead belongs to user
        lead = db.query(Lead).filter_by(id=lead_id, user_id=user.id).first()
        if not lead:
            await update.message.reply_text("Lead not found or does not belong to you. Please check /viewleads.")
            db.close()
            return ConversationHandler.END

        deal = Deal(user_id=user.id, lead_id=lead_id, title=context.user_data['deal_title'], 
                    value=float(update.message.text))
        db.add(deal)
        db.commit()
        await update.message.reply_text("✅ Deal added successfully!")
    except ValueError:
        await update.message.reply_text("Invalid lead ID or deal value. Please enter numbers.")
    except Exception as e:
        db.rollback()
        await update.message.reply_text(f"❌ Error adding deal: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

# --- Campaign Handlers ---
async def campaign_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)

    if not user.smtp_email or not user.smtp_password:
        await update.message.reply_text("Please set up your email credentials first using /setupmail before starting a campaign.")
        db.close()
        return ConversationHandler.END
    db.close()
    await update.message.reply_text("Enter a name for this campaign:")
    return START_CAMPAIGN_NAME

async def campaign_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['camp_name'] = update.message.text
    await update.message.reply_text("Enter Template ID to use (view /templates):")
    return START_CAMPAIGN_TEMPLATE

async def campaign_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)

    try:
        tmpl_id = int(update.message.text)
        # Verify template belongs to user
        template = db.query(Template).filter_by(id=tmpl_id, user_id=user.id).first()
        if not template:
            await update.message.reply_text("Template not found or does not belong to you. Please check /templates.")
            db.close()
            return ConversationHandler.END

        camp = Campaign(user_id=user.id, name=context.user_data['camp_name'], template_id=tmpl_id)
        db.add(camp)
        db.commit()
        camp_id = camp.id
        
        # Run campaign in background
        thread = threading.Thread(target=run_campaign, args=(SessionLocal(), camp_id))
        thread.start()
        
        await update.message.reply_text(f"🚀 Campaign '{camp.name}' started in background!")
    except ValueError:
        await update.message.reply_text("Invalid Template ID. Please enter a number.")
    except Exception as e:
        db.rollback()
        await update.message.reply_text(f"❌ Error starting campaign: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    db = SessionLocal()
    user = get_or_create_user(db, telegram_id)

    camps = db.query(Campaign).filter_by(user_id=user.id).all()
    if not camps:
        await update.message.reply_text("No campaigns found. Use /campaign to create one.")
        db.close()
        return
    text = "📊 *Campaign Stats:*\n\n"
    for c in camps:
        sent = db.query(CampaignLog).filter_by(campaign_id=c.id, user_id=user.id, status="sent").count()
        failed = db.query(CampaignLog).filter_by(campaign_id=c.id, user_id=user.id, status="failed").count()
        text += f"• {c.name}: {sent} Sent, {failed} Failed (Status: {c.status})\n"
    db.close()
    await update.message.reply_text(text, parse_mode='Markdown')

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Start ReplyChecker in a separate thread
    reply_checker = ReplyChecker(SessionLocal, TOKEN)
    reply_checker.start()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("viewleads", view_leads))
    app.add_handler(CommandHandler("deals", view_deals))
    app.add_handler(CommandHandler("templates", view_templates))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("mymail", my_mail))
    app.add_handler(CommandHandler("enablereplies", enable_replies))
    app.add_handler(CommandHandler("disablereplies", disable_replies))
    
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('addlead', add_lead_start)],
        states={ADD_LEAD_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_lead_email)],
                ADD_LEAD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_lead_name)],
                ADD_LEAD_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_lead_company)]},
        fallbacks=[CommandHandler('cancel', cancel)]))
    
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('importleads', import_leads_start)],
        states={IMPORT_LEADS: [MessageHandler(filters.Document.MimeType('text/csv') | filters.Document.ALL, import_leads_csv)]},
        fallbacks=[CommandHandler('cancel', cancel)]))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('addtemplate', add_template_start)],
        states={ADD_TEMPLATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_template_name)],
                ADD_TEMPLATE_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_template_subject)],
                ADD_TEMPLATE_BODY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_template_body)]},
        fallbacks=[CommandHandler('cancel', cancel)]))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('adddeal', add_deal_start)],
        states={ADD_DEAL_LEAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_deal_lead)],
                ADD_DEAL_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_deal_title)],
                ADD_DEAL_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_deal_value)]},
        fallbacks=[CommandHandler('cancel', cancel)]))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('campaign', campaign_start)],
        states={START_CAMPAIGN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_name)],
                START_CAMPAIGN_TEMPLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_template)]},
        fallbacks=[CommandHandler('cancel', cancel)]))
    
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('setupmail', setup_mail_start)],
        states={SETUP_MAIL_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_mail_email)],
                SETUP_MAIL_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_mail_password)]},
        fallbacks=[CommandHandler('cancel', cancel)]))
    
    print("Bot is running...")
    app.run_polling()
    # Ensure reply checker thread is stopped gracefully on bot shutdown
    reply_checker.stop()
    reply_checker.join()
