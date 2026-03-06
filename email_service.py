import smtplib
import time
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from sqlalchemy.orm import Session
from models import Lead, Template, Campaign, CampaignLog, User

logger = logging.getLogger(__name__)

def send_email(user_email, user_password, smtp_host, smtp_port, to_email, subject, body):
    """Sends a single email via SMTP using user-provided credentials."""
    try:
        msg = MIMEMultipart()
        msg["From"] = user_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls()
        server.login(user_email, user_password)
        server.send_message(msg)
        server.quit()
        return True, None
    except Exception as e:
        logger.error(f"Failed to send email to {to_email} using {user_email}: {str(e)}")
        return False, str(e)

def personalize_template(template_body, lead):
    """Replaces placeholders in the template with lead data."""
    placeholders = {
        "{first_name}": lead.first_name or "",
        "{last_name}": lead.last_name or "",
        "{company}": lead.company or "",
        "{email}": lead.email
    }
    body = template_body
    for key, value in placeholders.items():
        body = body.replace(key, str(value))
    return body

def run_campaign(db: Session, campaign_id: int, delay_seconds: int = 30):
    """Runs a campaign by sending emails to all leads for a specific user."""
    campaign = db.query(Campaign).filter_by(id=campaign_id).first()
    if not campaign:
        logger.error(f"Campaign with ID {campaign_id} not found.")
        return

    user = db.query(User).filter_by(id=campaign.user_id).first()
    if not user or not user.smtp_email or not user.smtp_password or not user.smtp_host or not user.smtp_port:
        logger.error(f"User {campaign.user_id} or their SMTP credentials not found for campaign {campaign_id}.")
        campaign.status = "failed"
        db.commit()
        return

    template = db.query(Template).filter_by(id=campaign.template_id, user_id=user.id).first()
    if not template:
        logger.error(f"Template with ID {campaign.template_id} not found for user {user.id}.")
        campaign.status = "failed"
        db.commit()
        return

    leads = db.query(Lead).filter_by(user_id=user.id).all()
    
    campaign.status = "running"
    db.commit()

    for lead in leads:
        # Check if already sent in this campaign
        if db.query(CampaignLog).filter_by(campaign_id=campaign_id, lead_id=lead.id).first():
            continue

        personalized_body = personalize_template(template.body, lead)
        success, error = send_email(
            user.smtp_email,
            user.smtp_password,
            user.smtp_host,
            user.smtp_port,
            lead.email,
            template.subject,
            personalized_body
        )
        
        log = CampaignLog(
            user_id=user.id,
            campaign_id=campaign_id,
            lead_id=lead.id,
            status="sent" if success else "failed",
            error_message=error
        )
        db.add(log)
        db.commit()
        
        if success:
            logger.info(f"Campaign {campaign_id}: Email sent to {lead.email} by user {user.telegram_id}")
        
        time.sleep(delay_seconds) # Avoid spam filters

    campaign.status = "completed"
    db.commit()
