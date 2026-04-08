import os
import logging
import datetime
import csv
import io
import threading
import re
import random

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool
from models import init_db, Lead, Template, Deal, Campaign, CampaignLog, User, Subscription, Payment
from email_service import run_campaign, check_subscription_limits, validate_email, test_smtp_connection, send_email, personalize_template, SUBSCRIPTION_PLANS
from reply_checker import ReplyChecker
from ai_writer import generate_cold_email, improve_email, generate_follow_up, suggest_subject_lines

# ==========================================
# CONFIGURATION - NO HARDCODED CREDENTIALS
# ==========================================
# CRITICAL FIX #1: Remove hardcoded fallback values
# These MUST be set in environment variables
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL", "sqlite:///bot_database.db")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")

# Validate required environment variables
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required!")
if not ADMIN_TELEGRAM_ID:
    raise ValueError("ADMIN_TELEGRAM_ID environment variable is required!")

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# DATABASE SETUP - THREAD-SAFE CONFIGURATION
# ==========================================
# CRITICAL FIX #2 & #3: Configure for thread safety
# For SQLite, we need special handling for multi-threading
if DB_URL.startswith("sqlite"):
    # SQLite with thread-safe configuration
    # check_same_thread=False allows usage across threads, but we must handle locking
    DB_URL = DB_URL.replace("sqlite:///", "sqlite:///?check_same_thread=False&")
    logger.warning("Using SQLite with multi-threading. Consider PostgreSQL for production.")

SessionLocal = init_db(DB_URL)

# ==========================================
# CONVERSATION STATES
# ==========================================
(SETUP_EMAIL, SETUP_PASSWORD, ADD_LEAD_EMAIL, ADD_LEAD_FIRST_NAME, ADD_LEAD_COMPANY,
 ADD_LEAD_INDUSTRY, ADD_LEAD_LOCATION, IMPORT_LEADS, ADD_TEMPLATE_NAME, ADD_TEMPLATE_SUBJECT,
 ADD_TEMPLATE_BODY, EDIT_TEMPLATE_SELECT, EDIT_TEMPLATE_FIELD, EDIT_TEMPLATE_VALUE,
 DELETE_TEMPLATE_SELECT, PREVIEW_TEMPLATE_SELECT, SELECT_CAMPAIGN_TEMPLATE, CAMPAIGN_NAME,
 DELETE_LEAD_SELECT, ADD_DEAL_TITLE, ADD_DEAL_VALUE, ADD_DEAL_LEAD, SUBSCRIBE_PROOF,
 ADMIN_BROADCAST, AI_WRITE_BUSINESS, AI_WRITE_AUDIENCE, AI_WRITE_OFFER, AI_WRITE_TONE,
 AI_IMPROVE_SELECT, AI_IMPROVE_CONFIRM, AI_FOLLOWUP_SELECT, AI_FOLLOWUP_NUMBER,
 AI_SUBJECTS_BUSINESS, AI_SUBJECTS_AUDIENCE) = range(34)

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def get_or_create_user(db: Session, telegram_id: int):
    """Get existing user or create new one with Free subscription."""
    user = db.query(User).filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id)
        db.add(user)
        db.commit()
        db.refresh(user)
        subscription = Subscription(
            user_id=user.id, plan_name="Free",
            start_date=datetime.datetime.utcnow(),
            end_date=datetime.datetime.utcnow() + datetime.timedelta(days=30),
            emails_sent_today=0, daily_limit=50, max_campaigns=2, is_active=True
        )
        db.add(subscription)
        db.commit()
        db.refresh(user)
    return user

def detect_email_settings(email_address):
    """Auto-detect SMTP/IMAP settings based on email domain."""
    domain = email_address.split('@')[-1].lower()
    if "gmail.com" in domain:
        return {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "imap_host": "imap.gmail.com", "imap_port": 993}
    elif "outlook.com" in domain or "hotmail.com" in domain or "office365.com" in domain:
        return {"smtp_host": "smtp.office365.com", "smtp_port": 587, "imap_host": "outlook.office365.com", "imap_port": 993}
    elif "yahoo.com" in domain:
        return {"smtp_host": "smtp.mail.yahoo.com", "smtp_port": 587, "imap_host": "imap.mail.yahoo.com", "imap_port": 993}
    else:
        return {"smtp_host": None, "smtp_port": None, "imap_host": None, "imap_port": None}

# ==========================================
# CAMPAIGN RUNNER - THREAD-SAFE IMPLEMENTATION
# ==========================================
def run_campaign_thread_safe(session_factory, camp_id):
    """
    CRITICAL FIX #2: Thread-safe campaign runner.
    Creates its own session inside the thread instead of sharing.
    """
    db = session_factory()  # Create session INSIDE the thread
    try:
        # Import run_campaign logic here or call it with the session
        # This ensures each thread has its own isolated session
        run_campaign(db, camp_id)
    except Exception as e:
        logger.error(f"Campaign {camp_id} failed in thread: {e}")
    finally:
        db.close()  # Always close session

def start_campaign_thread(session_factory, camp_id):
    """Start campaign in background thread with proper session handling."""
    thread = threading.Thread(
        target=run_campaign_thread_safe, 
        args=(session_factory, camp_id),  # Pass factory, not session instance
        daemon=True
    )
    thread.start()
    return thread

# ==========================================
# DASHBOARD & UI HANDLERS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main entry point - show dashboard menu."""
    db = SessionLocal()
    try:
        get_or_create_user(db, update.effective_user.id)
    finally:
        db.close()

    keyboard = [
        [InlineKeyboardButton("📥 Leads", callback_data="menu_leads"),
         InlineKeyboardButton("📄 Templates", callback_data="menu_templates")],
        [InlineKeyboardButton("🚀 Campaigns", callback_data="menu_campaigns"),
         InlineKeyboardButton("📊 Statistics", callback_data="menu_stats")],
        [InlineKeyboardButton("⚙ Settings", callback_data="menu_settings"),
         InlineKeyboardButton("💰 Subscription", callback_data="menu_subscription")],
        [InlineKeyboardButton("💼 Deals", callback_data="menu_deals"),
         InlineKeyboardButton("🤖 AI Writer", callback_data="menu_ai")]
    ]
    welcome = (
        "🚀 *Welcome to Cold Email Outreach Bot!*\n\n"
        "Manage leads, send cold emails, track deals & grow your business.\n"
        "🤖 NEW: AI-powered email writer to get more clients!\n\n"
        "Choose an option below or type /help for all commands."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(welcome, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(welcome, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all dashboard menu callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data

    back_btn = [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")]

    if data == "menu_main":
        await start_command(update, context)
        return

    elif data == "menu_leads":
        keyboard = [
            [InlineKeyboardButton("➕ Add Lead", callback_data="cmd_addlead"),
             InlineKeyboardButton("📋 View Leads", callback_data="cmd_viewleads")],
            [InlineKeyboardButton("📤 Import CSV", callback_data="cmd_importleads"),
             InlineKeyboardButton("📥 Export CSV", callback_data="cmd_exportleads")],
            [InlineKeyboardButton("🗑 Delete Lead", callback_data="cmd_deletelead"),
             InlineKeyboardButton("🧹 Clear All", callback_data="cmd_clearleads")],
            back_btn
        ]
        await query.edit_message_text("📥 *Lead Management*\n\nChoose an action:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "menu_templates":
        keyboard = [
            [InlineKeyboardButton("➕ Add Template", callback_data="cmd_addtemplate"),
             InlineKeyboardButton("📋 View Templates", callback_data="cmd_templates")],
            [InlineKeyboardButton("✏️ Edit Template", callback_data="cmd_edittemplate"),
             InlineKeyboardButton("🗑 Delete Template", callback_data="cmd_deletetemplate")],
            [InlineKeyboardButton("👁 Preview Template", callback_data="cmd_previewtemplate")],
            back_btn
        ]
        await query.edit_message_text("📄 *Template Management*\n\nChoose an action:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "menu_campaigns":
        keyboard = [
            [InlineKeyboardButton("🚀 Start Campaign", callback_data="cmd_campaign"),
             InlineKeyboardButton("📜 History", callback_data="cmd_history")],
            [InlineKeyboardButton("⏸ Pause", callback_data="cmd_pausecampaign"),
             InlineKeyboardButton("▶️ Resume", callback_data="cmd_resumecampaign")],
            [InlineKeyboardButton("❌ Cancel Campaign", callback_data="cmd_cancelcampaign"),
             InlineKeyboardButton("🗑 Delete Campaign", callback_data="cmd_deletecampaign")],
            back_btn
        ]
        await query.edit_message_text("🚀 *Campaign Management*\n\nChoose an action:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "menu_stats":
        await query.edit_message_text("Loading stats...", reply_markup=InlineKeyboardMarkup([back_btn]))
        telegram_id = update.effective_user.id
        db = SessionLocal()
        try:
            user = get_or_create_user(db, telegram_id)
            lead_count = db.query(Lead).filter_by(user_id=user.id).count()
            tmpl_count = db.query(Template).filter_by(user_id=user.id).count()
            camp_count = db.query(Campaign).filter_by(user_id=user.id).count()
            sent = db.query(CampaignLog).filter_by(user_id=user.id, status="sent").count()
            failed = db.query(CampaignLog).filter_by(user_id=user.id, status="failed").count()
            deal_count = db.query(Deal).filter_by(user_id=user.id).count()
            sub = db.query(Subscription).filter_by(user_id=user.id).first()
            text = (
                f"📊 *Your Statistics*\n\n"
                f"👤 Leads: {lead_count}\n"
                f"📄 Templates: {tmpl_count}\n"
                f"🚀 Campaigns: {camp_count}\n"
                f"✅ Emails Sent: {sent}\n"
                f"❌ Emails Failed: {failed}\n"
                f"💼 Deals: {deal_count}\n"
                f"📦 Plan: {sub.plan_name if sub else 'None'}\n"
                f"📧 Emails Today: {sub.emails_sent_today if sub else 0}/{sub.daily_limit if sub and sub.daily_limit != -1 else 'Unlimited'}"
            )
            await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([back_btn]))
        finally:
            db.close()

    elif data == "menu_settings":
        keyboard = [
            [InlineKeyboardButton("📧 Setup Mail", callback_data="cmd_setupmail"),
             InlineKeyboardButton("📬 My Mail", callback_data="cmd_mymail")],
            [InlineKeyboardButton("🔔 Enable Replies", callback_data="cmd_enablereplies"),
             InlineKeyboardButton("🔕 Disable Replies", callback_data="cmd_disablereplies")],
            back_btn
        ]
        await query.edit_message_text("⚙ *Settings*\n\nChoose an action:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "menu_subscription":
        keyboard = [
            [InlineKeyboardButton("📋 View Plans", callback_data="cmd_plans"),
             InlineKeyboardButton("📊 My Status", callback_data="cmd_mystatus")],
            [InlineKeyboardButton("💳 Subscribe", callback_data="cmd_subscribe")],
            back_btn
        ]
        await query.edit_message_text("💰 *Subscription*\n\nChoose an action:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "menu_deals":
        keyboard = [
            [InlineKeyboardButton("➕ Add Deal", callback_data="cmd_adddeal"),
             InlineKeyboardButton("📋 View Deals", callback_data="cmd_deals")],
            [InlineKeyboardButton("🔄 Update Deal", callback_data="cmd_updatedeal")],
            back_btn
        ]
        await query.edit_message_text("💼 *Deal Management*\n\nChoose an action:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "menu_ai":
        keyboard = [
            [InlineKeyboardButton("✍️ Write Cold Email", callback_data="cmd_aiwrite"),
             InlineKeyboardButton("✨ Improve Email", callback_data="cmd_aiimprove")],
            [InlineKeyboardButton("🔁 Generate Follow-up", callback_data="cmd_aifollowup"),
             InlineKeyboardButton("📌 Subject Lines", callback_data="cmd_aisubjects")],
            back_btn
        ]
        await query.edit_message_text(
            "🤖 *AI Email Writer*\n\n"
            "Let AI help you write cold emails that get more clients!\n\n"
            "✍️ *Write* - Generate a new cold email from scratch\n"
            "✨ *Improve* - Make your existing template better\n"
            "🔁 *Follow-up* - Create follow-up emails\n"
            "📌 *Subject Lines* - Get 5 subject line ideas",
            parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("cmd_"):
        cmd = data.replace("cmd_", "/")
        await query.edit_message_text(f"Please type {cmd} to use this feature.", reply_markup=InlineKeyboardMarkup([back_btn]))


# ==========================================
# HELP COMMAND
# ==========================================
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all available commands."""
    help_text = """📋 *All Available Commands:*

👤 *Leads*
/addlead - Add a lead manually
/importleads - Import leads via CSV
/viewleads - View all leads
/deletelead - Delete a lead
/clearleads - Clear all leads
/exportleads - Export leads as CSV

📄 *Templates*
/templates - View email templates
/addtemplate - Add a new template
/edittemplate - Edit a template
/deletetemplate - Delete a template
/previewtemplate - Preview a template

🚀 *Campaigns*
/campaign - Start a campaign
/pausecampaign - Pause a running campaign
/resumecampaign - Resume a paused campaign
/cancelcampaign - Cancel a campaign
/deletecampaign - Delete a campaign
/history - View campaign history

💼 *Deals*
/deals - View all deals
/adddeal - Add a new deal
/updatedeal - Update deal stage

📊 *Stats & Subscription*
/stats - View statistics
/plans - View subscription plans
/subscribe - Subscribe to a plan
/mystatus - View subscription status

🤖 *AI Email Writer*
/aiwrite - AI writes a cold email for you
/aiimprove - AI improves your existing template
/aifollowup - AI generates follow-up emails
/aisubjects - AI suggests 5 subject lines

⚙️ *Settings*
/setupmail - Setup email credentials
/mymail - View email settings
/enablereplies - Enable reply notifications
/disablereplies - Disable reply notifications
/help - Show this message
/cancel - Cancel current operation"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

# ==========================================
# LEAD MANAGEMENT
# ==========================================
async def add_lead_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start add lead conversation."""
    await update.message.reply_text("Enter lead's email address:")
    return ADD_LEAD_EMAIL

async def add_lead_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle lead email input."""
    email = update.message.text.strip()
    if not validate_email(email):
        await update.message.reply_text("❌ Invalid email format. Please enter a valid email or /cancel:")
        return ADD_LEAD_EMAIL
    context.user_data['lead_email'] = email
    await update.message.reply_text("Enter lead's first name:")
    return ADD_LEAD_FIRST_NAME

async def add_lead_first_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle lead first name input."""
    context.user_data['lead_first_name'] = update.message.text.strip()
    await update.message.reply_text("Enter lead's company:")
    return ADD_LEAD_COMPANY

async def add_lead_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle lead company input."""
    context.user_data['lead_company'] = update.message.text.strip()
    await update.message.reply_text("Enter lead's industry (or type 'skip'):")
    return ADD_LEAD_INDUSTRY

async def add_lead_industry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle lead industry input."""
    text = update.message.text.strip()
    context.user_data['lead_industry'] = text if text.lower() != 'skip' else ''
    await update.message.reply_text("Enter lead's location (or type 'skip'):")
    return ADD_LEAD_LOCATION

async def add_lead_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle lead location input and save lead."""
    text = update.message.text.strip()
    location = text if text.lower() != 'skip' else ''
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        new_lead = Lead(
            user_id=user.id, email=context.user_data['lead_email'],
            first_name=context.user_data['lead_first_name'],
            company=context.user_data['lead_company'],
            industry=context.user_data.get('lead_industry', ''),
            location=location
        )
        db.add(new_lead)
        db.commit()
        await update.message.reply_text(f"✅ Lead added: {new_lead.first_name} ({new_lead.email})")
    except Exception as e:
        db.rollback()
        logger.error(f"Error adding lead: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

async def import_leads_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start import leads conversation."""
    await update.message.reply_text("Upload a CSV file with columns: email, first_name, company, industry, location")
    return IMPORT_LEADS

async def import_leads_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle CSV file upload for lead import."""
    file = await update.message.document.get_file()
    content = await file.download_as_bytearray()
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        csv_file = io.StringIO(content.decode('utf-8'))
        reader = csv.DictReader(csv_file)
        count = 0
        skipped = 0
        for row in reader:
            email = row.get('email', '').strip()
            if not validate_email(email):
                skipped += 1
                continue
            if not db.query(Lead).filter_by(user_id=user.id, email=email).first():
                lead = Lead(
                    user_id=user.id, email=email,
                    first_name=row.get('first_name', ''),
                    last_name=row.get('last_name', ''),
                    company=row.get('company', ''),
                    industry=row.get('industry', ''),
                    location=row.get('location', '')
                )
                db.add(lead)
                count += 1
        db.commit()
        await update.message.reply_text(f"✅ Imported {count} leads. Skipped {skipped} invalid emails.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error importing leads: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

async def view_leads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display all user leads."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        leads = db.query(Lead).filter_by(user_id=user.id).all()
        if not leads:
            await update.message.reply_text("No leads found. Use /addlead or /importleads.")
            return
        text = "👤 *Your Leads:*\n\n"
        for i, l in enumerate(leads[:20], 1):
            text += f"{i}. {l.first_name or 'N/A'} - {l.email} ({l.company or '-'})\n"
        if len(leads) > 20:
            text += f"\n...and {len(leads) - 20} more. Total: {len(leads)}"
        await update.message.reply_text(text, parse_mode='Markdown')
    finally:
        db.close()

async def delete_lead_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start delete lead conversation."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        leads = db.query(Lead).filter_by(user_id=user.id).all()
        if not leads:
            await update.message.reply_text("No leads to delete.")
            return ConversationHandler.END
        text = "Enter the email of the lead to delete:\n\n"
        for l in leads[:15]:
            text += f"• {l.first_name or 'N/A'} - {l.email}\n"
        await update.message.reply_text(text)
        return DELETE_LEAD_SELECT
    finally:
        db.close()

async def delete_lead_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle lead deletion."""
    email = update.message.text.strip()
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        lead = db.query(Lead).filter_by(user_id=user.id, email=email).first()
        if lead:
            db.delete(lead)
            db.commit()
            await update.message.reply_text(f"✅ Lead {email} deleted.")
        else:
            await update.message.reply_text("❌ Lead not found.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting lead: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

async def clear_leads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all user leads."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        count = db.query(Lead).filter_by(user_id=user.id).delete()
        db.commit()
        await update.message.reply_text(f"🧹 Cleared {count} leads.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error clearing leads: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()

async def export_leads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export leads as CSV."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        leads = db.query(Lead).filter_by(user_id=user.id).all()
        if not leads:
            await update.message.reply_text("No leads to export.")
            return
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['email', 'first_name', 'last_name', 'company', 'industry', 'location'])
        for l in leads:
            writer.writerow([l.email, l.first_name or '', l.last_name or '', l.company or '', getattr(l, 'industry', ''), getattr(l, 'location', '')])
        output.seek(0)
        bio = io.BytesIO(output.getvalue().encode('utf-8'))
        bio.name = 'leads_export.csv'
        await update.message.reply_document(document=bio, filename='leads_export.csv', caption="📥 Your leads export")
    finally:
        db.close()


# ==========================================
# TEMPLATE MANAGEMENT
# ==========================================
async def view_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display all user templates."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        templates = db.query(Template).filter_by(user_id=user.id).all()
        if not templates:
            await update.message.reply_text("No templates. Use /addtemplate to create one.")
            return
        text = "📄 *Your Templates:*\n\n"
        for t in templates:
            text += f"ID: {t.id} | *{t.name}*\nSubject: {t.subject}\n\n"
        await update.message.reply_text(text, parse_mode='Markdown')
    finally:
        db.close()

async def add_template_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start add template conversation."""
    await update.message.reply_text("Enter template name:")
    return ADD_TEMPLATE_NAME

async def add_template_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle template name input."""
    context.user_data['tmpl_name'] = update.message.text.strip()
    await update.message.reply_text("Enter email subject:")
    return ADD_TEMPLATE_SUBJECT

async def add_template_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle template subject input."""
    context.user_data['tmpl_subject'] = update.message.text.strip()
    await update.message.reply_text(
        "Enter email body.\n\nAvailable variables:\n"
        "{first_name}, {company}, {email}, {industry}, {location}"
    )
    return ADD_TEMPLATE_BODY

async def add_template_body(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle template body input and save template."""
    body = update.message.text
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        tmpl = Template(user_id=user.id, name=context.user_data['tmpl_name'],
                       subject=context.user_data['tmpl_subject'], body=body)
        db.add(tmpl)
        db.commit()
        await update.message.reply_text("✅ Template saved!")
    except Exception as e:
        db.rollback()
        logger.error(f"Error saving template: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

async def edit_template_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start edit template conversation."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        templates = db.query(Template).filter_by(user_id=user.id).all()
        if not templates:
            await update.message.reply_text("No templates to edit.")
            return ConversationHandler.END
        text = "Enter Template ID to edit:\n\n"
        for t in templates:
            text += f"ID: {t.id} | {t.name}\n"
        await update.message.reply_text(text)
        return EDIT_TEMPLATE_SELECT
    finally:
        db.close()

async def edit_template_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle template selection for editing."""
    try:
        tmpl_id = int(update.message.text.strip())
        context.user_data['edit_tmpl_id'] = tmpl_id
        await update.message.reply_text("What to edit? Type: name, subject, or body")
        return EDIT_TEMPLATE_FIELD
    except ValueError:
        await update.message.reply_text("Invalid ID. Try again or /cancel.")
        return EDIT_TEMPLATE_SELECT

async def edit_template_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle field selection for editing."""
    field = update.message.text.strip().lower()
    if field not in ['name', 'subject', 'body']:
        await update.message.reply_text("Invalid field. Type: name, subject, or body")
        return EDIT_TEMPLATE_FIELD
    context.user_data['edit_tmpl_field'] = field
    await update.message.reply_text(f"Enter new {field}:")
    return EDIT_TEMPLATE_VALUE

async def edit_template_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new value input and update template."""
    new_value = update.message.text
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        tmpl = db.query(Template).filter_by(id=context.user_data['edit_tmpl_id'], user_id=user.id).first()
        if not tmpl:
            await update.message.reply_text("❌ Template not found.")
            return ConversationHandler.END
        field = context.user_data['edit_tmpl_field']
        setattr(tmpl, field, new_value)
        db.commit()
        await update.message.reply_text(f"✅ Template {field} updated!")
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating template: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

async def delete_template_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start delete template conversation."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        templates = db.query(Template).filter_by(user_id=user.id).all()
        if not templates:
            await update.message.reply_text("No templates to delete.")
            return ConversationHandler.END
        text = "Enter Template ID to delete:\n\n"
        for t in templates:
            text += f"ID: {t.id} | {t.name}\n"
        await update.message.reply_text(text)
        return DELETE_TEMPLATE_SELECT
    finally:
        db.close()

async def delete_template_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle template deletion."""
    try:
        tmpl_id = int(update.message.text.strip())
        telegram_id = update.effective_user.id
        db = SessionLocal()
        try:
            user = get_or_create_user(db, telegram_id)
            tmpl = db.query(Template).filter_by(id=tmpl_id, user_id=user.id).first()
            if tmpl:
                db.delete(tmpl)
                db.commit()
                await update.message.reply_text("✅ Template deleted!")
            else:
                await update.message.reply_text("❌ Template not found.")
        except Exception as e:
            db.rollback()
            logger.error(f"Error deleting template: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")
        finally:
            db.close()
    except ValueError:
        await update.message.reply_text("Invalid ID.")
    return ConversationHandler.END

async def preview_template_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start preview template conversation."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        templates = db.query(Template).filter_by(user_id=user.id).all()
        if not templates:
            await update.message.reply_text("No templates to preview.")
            return ConversationHandler.END
        text = "Enter Template ID to preview:\n\n"
        for t in templates:
            text += f"ID: {t.id} | {t.name}\n"
        await update.message.reply_text(text)
        return PREVIEW_TEMPLATE_SELECT
    finally:
        db.close()

async def preview_template_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle template preview."""
    try:
        tmpl_id = int(update.message.text.strip())
        telegram_id = update.effective_user.id
        db = SessionLocal()
        try:
            user = get_or_create_user(db, telegram_id)
            tmpl = db.query(Template).filter_by(id=tmpl_id, user_id=user.id).first()
            if not tmpl:
                await update.message.reply_text("❌ Template not found.")
                return ConversationHandler.END
            # Replace with example values
            preview_body = tmpl.body.replace("{first_name}", "John").replace("{company}", "Acme Corp")
            preview_body = preview_body.replace("{email}", "john@acme.com").replace("{industry}", "Technology")
            preview_body = preview_body.replace("{location}", "New York")
            preview_subject = tmpl.subject.replace("{first_name}", "John").replace("{company}", "Acme Corp")
            text = (
                f"👁 *Template Preview: {tmpl.name}*\n\n"
                f"*Subject:* {preview_subject}\n\n"
                f"*Body:*\n{preview_body}"
            )
            await update.message.reply_text(text, parse_mode='Markdown')
        finally:
            db.close()
    except ValueError:
        await update.message.reply_text("Invalid ID.")
    return ConversationHandler.END


# ==========================================
# CAMPAIGN MANAGEMENT - WITH THREAD SAFETY FIX
# ==========================================
async def campaign_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start campaign creation conversation."""
    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        can_create, msg = check_subscription_limits(db, user.id, "campaign_creation")
        if not can_create:
            await update.message.reply_text(msg)
            return ConversationHandler.END
        if not user.smtp_email or not user.smtp_password:
            await update.message.reply_text("Please set up email first with /setupmail.")
            return ConversationHandler.END
        templates = db.query(Template).filter_by(user_id=user.id).all()
        if not templates:
            await update.message.reply_text("No templates found. Create one with /addtemplate first.")
            return ConversationHandler.END
        text = "Select Template ID for this campaign:\n\n"
        for t in templates:
            text += f"ID: {t.id} | {t.name} | Subject: {t.subject}\n"
        await update.message.reply_text(text)
        return SELECT_CAMPAIGN_TEMPLATE
    finally:
        db.close()

async def campaign_select_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle template selection for campaign."""
    try:
        tmpl_id = int(update.message.text.strip())
        context.user_data['camp_template_id'] = tmpl_id
        await update.message.reply_text("Enter a name for this campaign:")
        return CAMPAIGN_NAME
    except ValueError:
        await update.message.reply_text("Invalid ID. Try again or /cancel.")
        return SELECT_CAMPAIGN_TEMPLATE

async def campaign_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle campaign name and start campaign with thread safety."""
    camp_name = update.message.text.strip()
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        tmpl_id = context.user_data['camp_template_id']
        template = db.query(Template).filter_by(id=tmpl_id, user_id=user.id).first()
        if not template:
            await update.message.reply_text("❌ Template not found.")
            return ConversationHandler.END

        # Test SMTP before starting
        await update.message.reply_text("🔄 Testing email connection...")
        success, error = test_smtp_connection(user.smtp_email, user.smtp_password, user.smtp_host, user.smtp_port)
        if not success:
            await update.message.reply_text(f"❌ SMTP test failed: {error}\n\nPlease check your email credentials with /setupmail.")
            return ConversationHandler.END

        camp = Campaign(user_id=user.id, name=camp_name, template_id=tmpl_id, status="pending")
        db.add(camp)
        db.commit()
        camp_id = camp.id
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating campaign: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
        return ConversationHandler.END
    finally:
        db.close()

    # CRITICAL FIX #2: Use thread-safe campaign runner
    # Pass SessionLocal factory, not a session instance
    start_campaign_thread(SessionLocal, camp_id)

    await update.message.reply_text(f"🚀 Campaign '{camp_name}' started! Emails sending with 10-20 second delays.\nCheck progress with /history.")
    return ConversationHandler.END

async def pause_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause running campaigns."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        camps = db.query(Campaign).filter_by(user_id=user.id, status="running").all()
        if not camps:
            await update.message.reply_text("No running campaigns to pause.")
            return
        for c in camps:
            c.status = "paused"
        db.commit()
        await update.message.reply_text(f"⏸ Paused {len(camps)} campaign(s).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error pausing campaign: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()

async def resume_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume paused campaigns with thread safety."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        camps = db.query(Campaign).filter_by(user_id=user.id, status="paused").all()
        if not camps:
            await update.message.reply_text("No paused campaigns to resume.")
            return
        for c in camps:
            c.status = "pending"
            db.commit()
            # CRITICAL FIX #2: Use thread-safe runner
            start_campaign_thread(SessionLocal, c.id)
        await update.message.reply_text(f"▶️ Resumed {len(camps)} campaign(s).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error resuming campaign: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()

async def cancel_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel active campaigns."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        camps = db.query(Campaign).filter(Campaign.user_id == user.id, Campaign.status.in_(["running", "paused", "pending"])).all()
        if not camps:
            await update.message.reply_text("No active campaigns to cancel.")
            return
        for c in camps:
            c.status = "cancelled"
        db.commit()
        await update.message.reply_text(f"❌ Cancelled {len(camps)} campaign(s).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error cancelling campaign: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()

async def delete_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete completed/cancelled campaigns."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        camps = db.query(Campaign).filter_by(user_id=user.id, status="completed").all()
        camps += db.query(Campaign).filter_by(user_id=user.id, status="cancelled").all()
        if not camps:
            await update.message.reply_text("No completed/cancelled campaigns to delete.")
            return
        count = 0
        for c in camps:
            db.query(CampaignLog).filter_by(campaign_id=c.id).delete()
            db.delete(c)
            count += 1
        db.commit()
        await update.message.reply_text(f"🗑 Deleted {count} campaign(s) and their logs.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting campaign: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show campaign history."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        camps = db.query(Campaign).filter_by(user_id=user.id).order_by(Campaign.created_at.desc()).limit(10).all()
        if not camps:
            await update.message.reply_text("No campaign history. Start one with /campaign.")
            return
        text = "📜 *Campaign History:*\n\n"
        for c in camps:
            sent = db.query(CampaignLog).filter_by(campaign_id=c.id, status="sent").count()
            failed = db.query(CampaignLog).filter_by(campaign_id=c.id, status="failed").count()
            text += f"*{c.name}* (ID: {c.id})\n"
            text += f"  Status: {c.status} | Sent: {sent} | Failed: {failed}\n"
            text += f"  Created: {c.created_at.strftime('%Y-%m-%d %H:%M') if c.created_at else 'N/A'}\n\n"
        await update.message.reply_text(text, parse_mode='Markdown')
    finally:
        db.close()


# ==========================================
# DEAL MANAGEMENT
# ==========================================
async def view_deals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display all user deals."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        deals = db.query(Deal).filter_by(user_id=user.id).all()
        if not deals:
            await update.message.reply_text("No deals. Use /adddeal to create one.")
            return
        text = "💼 *Your Deals:*\n\n"
        for d in deals:
            text += f"ID: {d.id} | {d.title} | ${d.value} | Stage: {d.stage}\n"
        await update.message.reply_text(text, parse_mode='Markdown')
    finally:
        db.close()

async def add_deal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start add deal conversation."""
    await update.message.reply_text("Enter deal title:")
    return ADD_DEAL_TITLE

async def add_deal_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle deal title input."""
    context.user_data['deal_title'] = update.message.text.strip()
    await update.message.reply_text("Enter deal value ($):")
    return ADD_DEAL_VALUE

async def add_deal_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle deal value input and save deal."""
    try:
        value = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Invalid number. Enter deal value ($):")
        return ADD_DEAL_VALUE
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        deal = Deal(user_id=user.id, title=context.user_data['deal_title'], value=value, stage="prospect")
        db.add(deal)
        db.commit()
        await update.message.reply_text(f"✅ Deal '{deal.title}' added! Value: ${value}")
    except Exception as e:
        db.rollback()
        logger.error(f"Error adding deal: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

async def update_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Update deal stage."""
    telegram_id = update.effective_user.id
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /updatedeal <deal_id> <stage>\n\n"
            "Stages: prospect, qualified, proposal, negotiation, closed_won, closed_lost"
        )
        return
    try:
        deal_id = int(context.args[0])
        stage = " ".join(context.args[1:]).lower().replace("_", " ")
        valid_stages = ["prospect", "qualified", "proposal", "negotiation", "closed won", "closed lost"]
        if stage not in valid_stages:
            await update.message.reply_text(f"Invalid stage. Choose from: {', '.join(valid_stages)}")
            return
        db = SessionLocal()
        try:
            user = get_or_create_user(db, telegram_id)
            deal = db.query(Deal).filter_by(id=deal_id, user_id=user.id).first()
            if not deal:
                await update.message.reply_text("Deal not found.")
                return
            deal.stage = stage
            deal.updated_at = datetime.datetime.utcnow()
            db.commit()
            await update.message.reply_text(f"✅ Deal '{deal.title}' updated to stage: {stage}")
        except Exception as e:
            db.rollback()
            logger.error(f"Error updating deal: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")
        finally:
            db.close()
    except ValueError:
        await update.message.reply_text("Invalid deal ID.")

# ==========================================
# STATISTICS
# ==========================================
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user statistics."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        lead_count = db.query(Lead).filter_by(user_id=user.id).count()
        tmpl_count = db.query(Template).filter_by(user_id=user.id).count()
        camp_count = db.query(Campaign).filter_by(user_id=user.id).count()
        sent = db.query(CampaignLog).filter_by(user_id=user.id, status="sent").count()
        failed = db.query(CampaignLog).filter_by(user_id=user.id, status="failed").count()
        deal_count = db.query(Deal).filter_by(user_id=user.id).count()
        sub = db.query(Subscription).filter_by(user_id=user.id).first()
        text = (
            f"📊 *Your Statistics*\n\n"
            f"👤 Leads: {lead_count}\n"
            f"📄 Templates: {tmpl_count}\n"
            f"🚀 Campaigns: {camp_count}\n"
            f"✅ Emails Sent: {sent}\n"
            f"❌ Emails Failed: {failed}\n"
            f"💼 Deals: {deal_count}\n\n"
            f"📦 Plan: {sub.plan_name if sub else 'None'}\n"
            f"📧 Emails Today: {sub.emails_sent_today if sub else 0}/{sub.daily_limit if sub and sub.daily_limit != -1 else 'Unlimited'}"
        )
        await update.message.reply_text(text, parse_mode='Markdown')
    finally:
        db.close()

# ==========================================
# MAIL SETUP
# ==========================================
async def setup_mail_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start email setup conversation."""
    await update.message.reply_text("Enter your email address (e.g., your@gmail.com):")
    return SETUP_EMAIL

async def setup_mail_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle email address input."""
    email = update.message.text.strip()
    if not validate_email(email):
        await update.message.reply_text("❌ Invalid email. Try again or /cancel:")
        return SETUP_EMAIL
    context.user_data['setup_email'] = email
    await update.message.reply_text("Enter your email password or App Password:")
    return SETUP_PASSWORD

async def setup_mail_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle password input and configure email."""
    password = update.message.text.strip()
    email_address = context.user_data['setup_email']
    telegram_id = update.effective_user.id
    settings = detect_email_settings(email_address)
    if not settings['smtp_host']:
        await update.message.reply_text("❌ Unsupported email provider. Use Gmail, Outlook, or Yahoo.")
        return ConversationHandler.END

    # Test SMTP connection
    await update.message.reply_text("🔄 Testing connection...")
    success, error = test_smtp_connection(email_address, password, settings['smtp_host'], settings['smtp_port'])
    if not success:
        await update.message.reply_text(f"❌ Connection failed: {error}\n\nFor Gmail, use an App Password (not regular password).")
        return ConversationHandler.END

    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        user.smtp_email = email_address
        user.smtp_password = password
        user.smtp_host = settings['smtp_host']
        user.smtp_port = settings['smtp_port']
        user.imap_host = settings['imap_host']
        user.imap_port = settings['imap_port']
        db.commit()
        await update.message.reply_text(f"✅ Email configured: {email_address}\nSMTP: {settings['smtp_host']}:{settings['smtp_port']}")
    except Exception as e:
        db.rollback()
        logger.error(f"Error setting up mail: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()
    return ConversationHandler.END

async def my_mail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current email settings."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        if user.smtp_email:
            status = "Enabled" if user.imap_enabled else "Disabled"
            text = (
                f"📧 *Your Email Settings:*\n\n"
                f"Email: {user.smtp_email}\n"
                f"SMTP: {user.smtp_host}:{user.smtp_port}\n"
                f"IMAP: {user.imap_host}:{user.imap_port}\n"
                f"Reply Checking: {status}"
            )
        else:
            text = "No email configured. Use /setupmail."
        await update.message.reply_text(text, parse_mode='Markdown')
    finally:
        db.close()

async def enable_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable reply notifications."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        if not user.smtp_email:
            await update.message.reply_text("Set up email first with /setupmail.")
            return
        user.imap_enabled = True
        db.commit()
        await update.message.reply_text("🔔 Reply notifications enabled!")
    except Exception as e:
        db.rollback()
        logger.error(f"Error enabling replies: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()

async def disable_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable reply notifications."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        user.imap_enabled = False
        db.commit()
        await update.message.reply_text("🔕 Reply notifications disabled.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error disabling replies: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        db.close()


# ==========================================
# SUBSCRIPTION MANAGEMENT
# ==========================================
async def plans_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show subscription plans."""
    text = "💰 *Subscription Plans:*\n\n"
    for plan_name, details in SUBSCRIPTION_PLANS.items():
        emails = details['emails_day'] if details['emails_day'] != -1 else 'Unlimited'
        camps = details['max_campaigns'] if details['max_campaigns'] != -1 else 'Unlimited'
        reply = '✅' if details['reply_detection'] else '❌'
        text += f"*{plan_name}* — ${details['price']}/month\n"
        text += f"  📧 {emails} emails/day | 🚀 {camps} campaigns | 🔔 Replies: {reply}\n\n"
    text += "To purchase a subscription, contact the admin on Telegram:\n👉 @Rishi1bitcoin"
    await update.message.reply_text(text, parse_mode='Markdown')

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show subscription instructions."""
    text = (
        "💳 *How to Subscribe:*\n\n"
        "To purchase a subscription, please contact the admin on Telegram:\n\n"
        "👉 @Rishi1bitcoin\n\n"
        "Send the admin your Telegram User ID and the plan you want.\n"
        f"Your Telegram ID: `{update.effective_user.id}`\n\n"
        "Available plans: /plans"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def mystatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show subscription status."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        sub = db.query(Subscription).filter_by(user_id=user.id).first()
        if not sub:
            await update.message.reply_text("No subscription. Use /plans to see options.")
            return
        text = (
            f"📊 *Your Subscription:*\n\n"
            f"Plan: {sub.plan_name}\n"
            f"Status: {'✅ Active' if sub.is_active else '❌ Inactive'}\n"
            f"Emails Today: {sub.emails_sent_today}/{sub.daily_limit if sub.daily_limit != -1 else 'Unlimited'}\n"
            f"Max Campaigns: {sub.max_campaigns if sub.max_campaigns != -1 else 'Unlimited'}\n"
            f"Expires: {sub.end_date.strftime('%Y-%m-%d') if sub.end_date else 'Never'}"
        )
        await update.message.reply_text(text, parse_mode='Markdown')
    finally:
        db.close()

# ==========================================
# ADMIN COMMANDS
# ==========================================
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin panel."""
    if str(update.effective_user.id) != str(ADMIN_TELEGRAM_ID):
        await update.message.reply_text("⛔ Not authorized.")
        return
    text = (
        "👑 *Admin Panel:*\n\n"
        "/activate <user_id> <plan> - Activate subscription\n"
        "/deactivate <user_id> - Deactivate subscription\n"
        "/blockuser <user_id> - Block a user\n"
        "/unblockuser <user_id> - Unblock a user\n"
        "/viewusers - View all users\n"
        "/revenue - View revenue\n"
        "/broadcast <message> - Message all users"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activate subscription for user."""
    if str(update.effective_user.id) != str(ADMIN_TELEGRAM_ID):
        await update.message.reply_text("⛔ Not authorized.")
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /activate <user_telegram_id> <plan_name>\nPlans: Free, Basic, Pro")
        return
    try:
        user_tid = int(context.args[0])
        plan_name = " ".join(context.args[1:])
        if plan_name not in SUBSCRIPTION_PLANS:
            await update.message.reply_text(f"Invalid plan. Available: {', '.join(SUBSCRIPTION_PLANS.keys())}")
            return
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_tid).first()
            if not user:
                await update.message.reply_text(f"User {user_tid} not found.")
                return
            plan = SUBSCRIPTION_PLANS[plan_name]
            sub = db.query(Subscription).filter_by(user_id=user.id).first()
            if sub:
                sub.plan_name = plan_name
                sub.start_date = datetime.datetime.utcnow()
                sub.end_date = None
                sub.emails_sent_today = 0
                sub.daily_limit = plan['emails_day']
                sub.max_campaigns = plan['max_campaigns']
                sub.is_active = True
            else:
                sub = Subscription(
                    user_id=user.id, plan_name=plan_name,
                    start_date=datetime.datetime.utcnow(),
                    emails_sent_today=0, daily_limit=plan['emails_day'],
                    max_campaigns=plan['max_campaigns'], is_active=True
                )
                db.add(sub)
            db.commit()
            await update.message.reply_text(f"✅ Activated {plan_name} for user {user_tid}.")
            try:
                await context.bot.send_message(chat_id=user_tid, text=f"🎉 Your {plan_name} subscription has been activated!")
            except Exception as e:
                logger.warning(f"Could not notify user {user_tid}: {e}")
        except Exception as e:
            db.rollback()
            logger.error(f"Error activating subscription: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Error in activate command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def deactivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deactivate subscription for user."""
    if str(update.effective_user.id) != str(ADMIN_TELEGRAM_ID):
        await update.message.reply_text("⛔ Not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /deactivate <user_telegram_id>")
        return
    try:
        user_tid = int(context.args[0])
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_tid).first()
            if not user:
                await update.message.reply_text(f"User {user_tid} not found.")
                return
            sub = db.query(Subscription).filter_by(user_id=user.id).first()
            if sub:
                sub.is_active = False
                db.commit()
                await update.message.reply_text(f"✅ Deactivated subscription for {user_tid}.")
            else:
                await update.message.reply_text("No subscription found.")
        except Exception as e:
            db.rollback()
            logger.error(f"Error deactivating subscription: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Error in deactivate command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def block_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Block a user."""
    if str(update.effective_user.id) != str(ADMIN_TELEGRAM_ID):
        await update.message.reply_text("⛔ Not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /blockuser <user_telegram_id>")
        return
    try:
        user_tid = int(context.args[0])
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_tid).first()
            if user:
                user.is_blocked = True
                db.commit()
                await update.message.reply_text(f"🚫 User {user_tid} blocked.")
            else:
                await update.message.reply_text("User not found.")
        except Exception as e:
            db.rollback()
            logger.error(f"Error blocking user: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Error in block command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def unblock_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unblock a user."""
    if str(update.effective_user.id) != str(ADMIN_TELEGRAM_ID):
        await update.message.reply_text("⛔ Not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unblockuser <user_telegram_id>")
        return
    try:
        user_tid = int(context.args[0])
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_tid).first()
            if user:
                user.is_blocked = False
                db.commit()
                await update.message.reply_text(f"✅ User {user_tid} unblocked.")
            else:
                await update.message.reply_text("User not found.")
        except Exception as e:
            db.rollback()
            logger.error(f"Error unblocking user: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Error in unblock command: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def view_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View all users (admin only)."""
    if str(update.effective_user.id) != str(ADMIN_TELEGRAM_ID):
        await update.message.reply_text("⛔ Not authorized.")
        return
    db = SessionLocal()
    try:
        users = db.query(User).all()
        text = "*All Users:*\n\n"
        for u in users:
            sub = db.query(Subscription).filter_by(user_id=u.id).first()
            blocked = " 🚫BLOCKED" if getattr(u, 'is_blocked', False) else ""
            text += f"ID: {u.telegram_id}{blocked}\n"
            if sub:
                text += f"  Plan: {sub.plan_name} ({'Active' if sub.is_active else 'Inactive'})\n"
            else:
                text += "  No subscription\n"
            text += "\n"
        await update.message.reply_text(text, parse_mode='Markdown')
    finally:
        db.close()

async def revenue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View revenue (admin only)."""
    if str(update.effective_user.id) != str(ADMIN_TELEGRAM_ID):
        await update.message.reply_text("⛔ Not authorized.")
        return
    db = SessionLocal()
    try:
        payments = db.query(Payment).filter_by(status="approved").all()
        total = sum(float(p.amount) for p in payments)
        user_count = db.query(User).count()
        active_subs = db.query(Subscription).filter_by(is_active=True).count()
        text = (
            f"💰 *Revenue Dashboard:*\n\n"
            f"Total Revenue: ${total:.2f}\n"
            f"Total Users: {user_count}\n"
            f"Active Subscriptions: {active_subs}"
        )
        await update.message.reply_text(text, parse_mode='Markdown')
    finally:
        db.close()

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users (admin only)."""
    if str(update.effective_user.id) != str(ADMIN_TELEGRAM_ID):
        await update.message.reply_text("⛔ Not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    message_text = " ".join(context.args)
    db = SessionLocal()
    try:
        users = db.query(User).all()
        sent = 0
        failed = 0
        for u in users:
            try:
                await context.bot.send_message(chat_id=u.telegram_id, text=f"📢 {message_text}")
                sent += 1
            except Exception as e:
                logger.warning(f"Failed to send broadcast to {u.telegram_id}: {e}")
                failed += 1
        await update.message.reply_text(f"✅ Broadcast sent to {sent} users. Failed: {failed}.")
    finally:
        db.close()


# ==========================================
# AI EMAIL WRITER
# ==========================================
async def ai_write_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start AI email writing conversation."""
    await update.message.reply_text(
        "🤖 *AI Cold Email Writer*\n\n"
        "I'll write a high-converting cold email for you!\n\n"
        "First, what is your business or service?\n"
        "(e.g., Web development agency, Marketing consultant, SaaS product)",
        parse_mode='Markdown'
    )
    return AI_WRITE_BUSINESS

async def ai_write_business(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle business description input."""
    context.user_data['ai_business'] = update.message.text.strip()
    await update.message.reply_text(
        "Who is your target audience?\n"
        "(e.g., Startup founders, E-commerce store owners, Real estate agents)"
    )
    return AI_WRITE_AUDIENCE

async def ai_write_audience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle audience input."""
    context.user_data['ai_audience'] = update.message.text.strip()
    await update.message.reply_text(
        "What are you offering them?\n"
        "(e.g., Free website audit, 50% discount on first project, Free consultation call)"
    )
    return AI_WRITE_OFFER

async def ai_write_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle offer input and show tone options."""
    context.user_data['ai_offer'] = update.message.text.strip()
    keyboard = [
        [InlineKeyboardButton("Professional", callback_data="tone_professional"),
         InlineKeyboardButton("Friendly", callback_data="tone_friendly")],
        [InlineKeyboardButton("Casual", callback_data="tone_casual"),
         InlineKeyboardButton("Urgent", callback_data="tone_urgent")]
    ]
    await update.message.reply_text(
        "Choose the email tone:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return AI_WRITE_TONE

async def ai_write_tone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle tone selection and generate email."""
    query = update.callback_query
    await query.answer()
    tone = query.data.replace("tone_", "")

    await query.edit_message_text("🤖 AI is writing your cold email... Please wait.")

    result = generate_cold_email(
        context.user_data['ai_business'],
        context.user_data['ai_audience'],
        context.user_data['ai_offer'],
        tone
    )

    if result['success']:
        keyboard = [
            [InlineKeyboardButton("💾 Save as Template", callback_data="ai_save_template")],
            [InlineKeyboardButton("🔄 Regenerate", callback_data=f"tone_{tone}")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")]
        ]
        context.user_data['ai_generated_subject'] = result['subject']
        context.user_data['ai_generated_body'] = result['body']
        await query.edit_message_text(
            f"🤖 *AI Generated Cold Email:*\n\n"
            f"📧 *Subject:* {result['subject']}\n\n"
            f"📝 *Body:*\n{result['body']}\n\n"
            f"_Use /aiwrite to generate another or save this as a template._",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await query.edit_message_text(f"❌ AI Error: {result['error']}\n\nTry again with /aiwrite")

    return ConversationHandler.END

async def ai_save_template_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save AI-generated email as template."""
    query = update.callback_query
    await query.answer()

    subject = context.user_data.get('ai_generated_subject', '')
    body = context.user_data.get('ai_generated_body', '')

    if not subject or not body:
        await query.edit_message_text("❌ No AI-generated email found. Use /aiwrite first.")
        return

    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        tmpl = Template(
            user_id=user.id,
            name=f"AI Generated - {context.user_data.get('ai_business', 'Cold Email')[:30]}",
            subject=subject,
            body=body
        )
        db.add(tmpl)
        db.commit()
        await query.edit_message_text(
            f"✅ Template saved!\n\n"
            f"Name: {tmpl.name}\n"
            f"ID: {tmpl.id}\n\n"
            f"You can now use this in your campaigns with /campaign"
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Error saving AI template: {e}")
        await query.edit_message_text(f"❌ Error saving: {str(e)}")
    finally:
        db.close()

async def ai_improve_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start AI email improvement conversation."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        templates = db.query(Template).filter_by(user_id=user.id).all()
        if not templates:
            await update.message.reply_text("No templates to improve. Create one first with /addtemplate or /aiwrite")
            return ConversationHandler.END
        text = "🤖 *AI Email Improver*\n\nSelect a template to improve (enter ID):\n\n"
        for t in templates:
            text += f"ID: {t.id} | *{t.name}*\n"
        await update.message.reply_text(text, parse_mode='Markdown')
        return AI_IMPROVE_SELECT
    finally:
        db.close()

async def ai_improve_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle template selection for improvement."""
    try:
        tmpl_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Invalid ID. Try again or /cancel.")
        return AI_IMPROVE_SELECT

    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        tmpl = db.query(Template).filter_by(id=tmpl_id, user_id=user.id).first()
        if not tmpl:
            await update.message.reply_text("Template not found. Try again or /cancel.")
            return AI_IMPROVE_SELECT

        await update.message.reply_text("🤖 AI is improving your email... Please wait.")

        result = improve_email(tmpl.subject, tmpl.body)

        if result['success']:
            context.user_data['ai_generated_subject'] = result['subject']
            context.user_data['ai_generated_body'] = result['body']
            keyboard = [
                [InlineKeyboardButton("💾 Save as New Template", callback_data="ai_save_template")],
                [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")]
            ]
            await update.message.reply_text(
                f"✨ *AI Improved Email:*\n\n"
                f"📧 *Subject:* {result['subject']}\n\n"
                f"📝 *Body:*\n{result['body']}\n\n"
                f"_Save this as a new template or go back._",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(f"❌ AI Error: {result['error']}\n\nTry again with /aiimprove")
    finally:
        db.close()

    return ConversationHandler.END

async def ai_followup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start AI follow-up generation conversation."""
    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        templates = db.query(Template).filter_by(user_id=user.id).all()
        if not templates:
            await update.message.reply_text("No templates found. Create one first with /addtemplate or /aiwrite")
            return ConversationHandler.END
        text = "🔁 *AI Follow-up Generator*\n\nSelect the original template (enter ID):\n\n"
        for t in templates:
            text += f"ID: {t.id} | *{t.name}*\n"
        await update.message.reply_text(text, parse_mode='Markdown')
        return AI_FOLLOWUP_SELECT
    finally:
        db.close()

async def ai_followup_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle template selection for follow-up."""
    try:
        tmpl_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Invalid ID. Try again or /cancel.")
        return AI_FOLLOWUP_SELECT

    telegram_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id)
        tmpl = db.query(Template).filter_by(id=tmpl_id, user_id=user.id).first()
        if not tmpl:
            await update.message.reply_text("Template not found. Try again or /cancel.")
            return AI_FOLLOWUP_SELECT

        context.user_data['followup_subject'] = tmpl.subject
        context.user_data['followup_body'] = tmpl.body
        await update.message.reply_text(
            "Which follow-up number?\n\n"
            "1 - First follow-up (casual, brief)\n"
            "2 - Second follow-up (new angle)\n"
            "3 - Final follow-up (breakup email)"
        )
        return AI_FOLLOWUP_NUMBER
    finally:
        db.close()

async def ai_followup_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle follow-up number selection and generate."""
    try:
        num = int(update.message.text.strip())
        if num not in [1, 2, 3]:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter 1, 2, or 3. Or /cancel.")
        return AI_FOLLOWUP_NUMBER

    await update.message.reply_text("🤖 AI is generating your follow-up email... Please wait.")

    result = generate_follow_up(
        context.user_data['followup_subject'],
        context.user_data['followup_body'],
        num
    )

    if result['success']:
        context.user_data['ai_generated_subject'] = result['subject']
        context.user_data['ai_generated_body'] = result['body']
        keyboard = [
            [InlineKeyboardButton("💾 Save as Template", callback_data="ai_save_template")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")]
        ]
        await update.message.reply_text(
            f"🔁 *AI Follow-up #{num}:*\n\n"
            f"📧 *Subject:* {result['subject']}\n\n"
            f"📝 *Body:*\n{result['body']}\n\n"
            f"_Save this as a template to use in campaigns._",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(f"❌ AI Error: {result['error']}\n\nTry again with /aifollowup")

    return ConversationHandler.END

async def ai_subjects_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start AI subject line generation."""
    await update.message.reply_text(
        "📌 *AI Subject Line Generator*\n\n"
        "What is your business or service?\n"
        "(e.g., Web development, Digital marketing, Consulting)",
        parse_mode='Markdown'
    )
    return AI_SUBJECTS_BUSINESS

async def ai_subjects_business(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle business input for subject lines."""
    context.user_data['subjects_business'] = update.message.text.strip()
    await update.message.reply_text(
        "Who is your target audience?\n"
        "(e.g., SaaS founders, Small business owners, HR managers)"
    )
    return AI_SUBJECTS_AUDIENCE

async def ai_subjects_audience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle audience input and generate subject lines."""
    audience = update.message.text.strip()

    await update.message.reply_text("🤖 AI is generating subject lines... Please wait.")

    result = suggest_subject_lines(
        context.user_data['subjects_business'],
        audience
    )

    if result['success']:
        await update.message.reply_text(
            f"📌 *AI Subject Line Suggestions:*\n\n"
            f"{result['subject_lines']}\n\n"
            f"_Use these in your templates! Create a template with /addtemplate_",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(f"❌ AI Error: {result['error']}\n\nTry again with /aisubjects")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel current conversation."""
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

# ==========================================
# DAILY RESET JOB
# ==========================================
async def reset_daily_emails(context: ContextTypes.DEFAULT_TYPE):
    """Reset daily email counts at midnight."""
    db = SessionLocal()
    try:
        subs = db.query(Subscription).filter_by(is_active=True).all()
        for s in subs:
            s.emails_sent_today = 0
        db.commit()
        logger.info("Daily email counts reset.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error resetting daily emails: {e}")
    finally:
        db.close()

# ==========================================
# MAIN ENTRY POINT - WITH ERROR HANDLING
# ==========================================
async def post_stop_callback(application):
    """Cleanup function called after bot stops."""
    logger.info("Running post-stop cleanup...")
    # Stop reply checker if it exists
    if hasattr(application, '_reply_checker') and application._reply_checker:
        logger.info("Stopping reply checker...")
        application._reply_checker.stop()
        application._reply_checker.join(timeout=5)
        logger.info("Reply checker stopped")

def main():
    """Main function with proper error handling and cleanup."""
    reply_checker = None

    try:
        # Start ReplyChecker in daemon thread BEFORE building app
        reply_checker = ReplyChecker(SessionLocal, TOKEN)
        reply_checker.daemon = True
        reply_checker.start()
        logger.info("Reply checker started")

        # Build application with post_stop callback for cleanup
        app = (
            ApplicationBuilder()
            .token(TOKEN)
            .post_stop(post_stop_callback)
            .build()
        )
        # Store reply_checker reference in app for access in post_stop
        app._reply_checker = reply_checker

        # ==========================================
        # CONVERSATION HANDLERS
        # ==========================================

        # Add Lead Conversation
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('addlead', add_lead_start)],
            states={
                ADD_LEAD_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_lead_email)],
                ADD_LEAD_FIRST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_lead_first_name)],
                ADD_LEAD_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_lead_company)],
                ADD_LEAD_INDUSTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_lead_industry)],
                ADD_LEAD_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_lead_location)],
            },
            fallbacks=[CommandHandler('cancel', cancel)]))

        # Import Leads Conversation
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('importleads', import_leads_start)],
            states={IMPORT_LEADS: [MessageHandler(filters.Document.ALL, import_leads_csv)]},
            fallbacks=[CommandHandler('cancel', cancel)]))

        # Delete Lead Conversation
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('deletelead', delete_lead_start)],
            states={DELETE_LEAD_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_lead_select)]},
            fallbacks=[CommandHandler('cancel', cancel)]))

        # Add Template Conversation
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('addtemplate', add_template_start)],
            states={
                ADD_TEMPLATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_template_name)],
                ADD_TEMPLATE_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_template_subject)],
                ADD_TEMPLATE_BODY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_template_body)],
            },
            fallbacks=[CommandHandler('cancel', cancel)]))

        # Edit Template Conversation
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('edittemplate', edit_template_start)],
            states={
                EDIT_TEMPLATE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_template_select)],
                EDIT_TEMPLATE_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_template_field)],
                EDIT_TEMPLATE_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_template_value)],
            },
            fallbacks=[CommandHandler('cancel', cancel)]))

        # Delete Template Conversation
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('deletetemplate', delete_template_start)],
            states={DELETE_TEMPLATE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_template_select)]},
            fallbacks=[CommandHandler('cancel', cancel)]))

        # Preview Template Conversation
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('previewtemplate', preview_template_start)],
            states={PREVIEW_TEMPLATE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, preview_template_select)]},
            fallbacks=[CommandHandler('cancel', cancel)]))

        # Campaign Conversation
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('campaign', campaign_start)],
            states={
                SELECT_CAMPAIGN_TEMPLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_select_template)],
                CAMPAIGN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_name)],
            },
            fallbacks=[CommandHandler('cancel', cancel)]))

        # Setup Mail Conversation
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('setupmail', setup_mail_start)],
            states={
                SETUP_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_mail_email)],
                SETUP_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_mail_password)],
            },
            fallbacks=[CommandHandler('cancel', cancel)]))

        # Add Deal Conversation
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('adddeal', add_deal_start)],
            states={
                ADD_DEAL_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_deal_title)],
                ADD_DEAL_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_deal_value)],
            },
            fallbacks=[CommandHandler('cancel', cancel)]))

        # AI Writer Conversations
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('aiwrite', ai_write_start)],
            states={
                AI_WRITE_BUSINESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_write_business)],
                AI_WRITE_AUDIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_write_audience)],
                AI_WRITE_OFFER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_write_offer)],
                AI_WRITE_TONE: [CallbackQueryHandler(ai_write_tone_callback, pattern='^tone_')],
            },
            fallbacks=[CommandHandler('cancel', cancel)]))

        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('aiimprove', ai_improve_start)],
            states={
                AI_IMPROVE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_improve_select)],
            },
            fallbacks=[CommandHandler('cancel', cancel)]))

        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('aifollowup', ai_followup_start)],
            states={
                AI_FOLLOWUP_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_followup_select)],
                AI_FOLLOWUP_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_followup_number)],
            },
            fallbacks=[CommandHandler('cancel', cancel)]))

        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler('aisubjects', ai_subjects_start)],
            states={
                AI_SUBJECTS_BUSINESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_subjects_business)],
                AI_SUBJECTS_AUDIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_subjects_audience)],
            },
            fallbacks=[CommandHandler('cancel', cancel)]))

        # AI save template callback
        app.add_handler(CallbackQueryHandler(ai_save_template_callback, pattern='^ai_save_template$'))

        # ==========================================
        # COMMAND HANDLERS
        # ==========================================
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("viewleads", view_leads))
        app.add_handler(CommandHandler("clearleads", clear_leads))
        app.add_handler(CommandHandler("exportleads", export_leads))
        app.add_handler(CommandHandler("templates", view_templates))
        app.add_handler(CommandHandler("pausecampaign", pause_campaign))
        app.add_handler(CommandHandler("resumecampaign", resume_campaign))
        app.add_handler(CommandHandler("cancelcampaign", cancel_campaign))
        app.add_handler(CommandHandler("deletecampaign", delete_campaign))
        app.add_handler(CommandHandler("history", history_command))
        app.add_handler(CommandHandler("deals", view_deals))
        app.add_handler(CommandHandler("updatedeal", update_deal))
        app.add_handler(CommandHandler("stats", stats_command))
        app.add_handler(CommandHandler("mymail", my_mail))
        app.add_handler(CommandHandler("enablereplies", enable_replies))
        app.add_handler(CommandHandler("disablereplies", disable_replies))
        app.add_handler(CommandHandler("plans", plans_command))
        app.add_handler(CommandHandler("subscribe", subscribe_command))
        app.add_handler(CommandHandler("mystatus", mystatus_command))
        app.add_handler(CommandHandler("admin", admin_command))
        app.add_handler(CommandHandler("activate", activate_command))
        app.add_handler(CommandHandler("deactivate", deactivate_command))
        app.add_handler(CommandHandler("blockuser", block_user_command))
        app.add_handler(CommandHandler("unblockuser", unblock_user_command))
        app.add_handler(CommandHandler("viewusers", view_users_command))
        app.add_handler(CommandHandler("revenue", revenue_command))
        app.add_handler(CommandHandler("broadcast", broadcast_command))

        # Dashboard callback handler
        app.add_handler(CallbackQueryHandler(dashboard_callback))

        # ==========================================
        # SCHEDULED JOBS
        # ==========================================
        job_queue = app.job_queue
        if job_queue:
            job_queue.run_daily(reset_daily_emails, time=datetime.time(hour=0, minute=0, second=0))
            logger.info("Daily reset job scheduled")

        logger.info("🚀 Bot is starting...")

        # Start polling
        app.run_polling()

    except Exception as e:
        logger.critical(f"Bot crashed: {e}", exc_info=True)
        raise
    except Exception as e:
        logger.critical(f"Bot crashed during startup: {e}", exc_info=True)
        # Cleanup on startup failure
        if reply_checker and reply_checker.is_alive():
            reply_checker.stop()
            reply_checker.join(timeout=5)
        raise

if __name__ == "__main__":
    main()
