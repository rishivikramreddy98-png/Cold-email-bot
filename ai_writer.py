import os
import logging
from openai import OpenAI

logger = logging.getLogger(__name__)

# Use the pre-configured OpenAI-compatible API
client = OpenAI()

def generate_cold_email(business_type: str, target_audience: str, offer: str, tone: str = "professional") -> dict:
    """Generate a cold email subject and body using AI."""
    prompt = f"""You are an expert cold email copywriter who specializes in writing emails that get high open rates and replies.

Write a cold email with the following details:
- Business/Service: {business_type}
- Target Audience: {target_audience}
- What we're offering: {offer}
- Tone: {tone}

Requirements:
1. Subject line should be short, curiosity-driven, and personalized with {{first_name}} placeholder
2. Email body should be concise (under 150 words)
3. Use {{first_name}}, {{company}}, {{industry}} placeholders where appropriate
4. Include a clear call-to-action
5. Make it feel personal, not salesy
6. Don't use generic openers like "I hope this email finds you well"

Return ONLY in this exact format:
SUBJECT: [your subject line]
BODY:
[your email body]"""

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": "You are an expert cold email copywriter. Write concise, high-converting cold emails."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.8,
            max_tokens=500
        )
        
        result = response.choices[0].message.content.strip()
        
        # Parse subject and body
        subject = ""
        body = ""
        if "SUBJECT:" in result and "BODY:" in result:
            parts = result.split("BODY:", 1)
            subject = parts[0].replace("SUBJECT:", "").strip()
            body = parts[1].strip()
        else:
            # Fallback: first line is subject, rest is body
            lines = result.split("\n", 1)
            subject = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else result
        
        return {"success": True, "subject": subject, "body": body}
    
    except Exception as e:
        logger.error(f"AI email generation error: {e}")
        return {"success": False, "error": str(e)}


def improve_email(subject: str, body: str) -> dict:
    """Improve an existing cold email using AI."""
    prompt = f"""You are an expert cold email copywriter. Improve this cold email to get higher open rates and more replies.

Current Subject: {subject}
Current Body:
{body}

Requirements:
1. Make the subject line more compelling and curiosity-driven
2. Make the body more concise and personal
3. Improve the call-to-action
4. Keep {{first_name}}, {{company}}, {{industry}} placeholders if present
5. Keep it under 150 words
6. Remove any generic/salesy language

Return ONLY in this exact format:
SUBJECT: [improved subject line]
BODY:
[improved email body]"""

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": "You are an expert cold email copywriter. Improve emails to be more effective."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=500
        )
        
        result = response.choices[0].message.content.strip()
        
        subject = ""
        body = ""
        if "SUBJECT:" in result and "BODY:" in result:
            parts = result.split("BODY:", 1)
            subject = parts[0].replace("SUBJECT:", "").strip()
            body = parts[1].strip()
        else:
            lines = result.split("\n", 1)
            subject = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else result
        
        return {"success": True, "subject": subject, "body": body}
    
    except Exception as e:
        logger.error(f"AI email improvement error: {e}")
        return {"success": False, "error": str(e)}


def generate_follow_up(original_subject: str, original_body: str, follow_up_number: int = 1) -> dict:
    """Generate a follow-up email based on the original."""
    prompt = f"""You are an expert cold email copywriter. Write follow-up email #{follow_up_number} for this cold email that didn't get a reply.

Original Subject: {original_subject}
Original Email:
{original_body}

Requirements:
1. Reference the previous email naturally
2. Add new value or angle
3. Keep it very short (under 80 words)
4. Include a soft call-to-action
5. Don't be pushy or desperate
6. Keep {{first_name}}, {{company}} placeholders if present
7. For follow-up #{follow_up_number}, {"be casual and brief" if follow_up_number == 1 else "try a different angle or offer something valuable" if follow_up_number == 2 else "make it a breakup email - last chance"}

Return ONLY in this exact format:
SUBJECT: [follow-up subject line]
BODY:
[follow-up email body]"""

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": "You are an expert cold email copywriter specializing in follow-up emails."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.8,
            max_tokens=400
        )
        
        result = response.choices[0].message.content.strip()
        
        subject = ""
        body = ""
        if "SUBJECT:" in result and "BODY:" in result:
            parts = result.split("BODY:", 1)
            subject = parts[0].replace("SUBJECT:", "").strip()
            body = parts[1].strip()
        else:
            lines = result.split("\n", 1)
            subject = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else result
        
        return {"success": True, "subject": subject, "body": body}
    
    except Exception as e:
        logger.error(f"AI follow-up generation error: {e}")
        return {"success": False, "error": str(e)}


def suggest_subject_lines(business_type: str, target_audience: str) -> dict:
    """Generate multiple subject line options."""
    prompt = f"""You are an expert cold email copywriter. Generate 5 compelling subject lines for cold emails.

Business/Service: {business_type}
Target Audience: {target_audience}

Requirements:
1. Each subject line should be under 50 characters
2. Use {{first_name}} placeholder in at least 2
3. Mix different styles: curiosity, question, value-driven, personal
4. No clickbait or spam triggers

Return ONLY numbered list:
1. [subject line]
2. [subject line]
3. [subject line]
4. [subject line]
5. [subject line]"""

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": "You are an expert at writing email subject lines that get opened."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9,
            max_tokens=300
        )
        
        result = response.choices[0].message.content.strip()
        return {"success": True, "subject_lines": result}
    
    except Exception as e:
        logger.error(f"AI subject line generation error: {e}")
        return {"success": False, "error": str(e)}
