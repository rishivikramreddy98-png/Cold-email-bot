"""
Business Lead Finder Module
Searches public directories and web sources to find business leads
based on niche and location.
"""

import re
import logging
import requests
from bs4 import BeautifulSoup
import time
import random
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

def extract_emails(text):
    """Extract email addresses from text."""
    pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = re.findall(pattern, text)
    # Filter out common false positives
    filtered = []
    for e in emails:
        e_lower = e.lower()
        if not any(x in e_lower for x in ['example.com', 'domain.com', 'email.com', 'yoursite.com',
                                            'sentry.io', 'wixpress.com', 'googleapis.com', '.png',
                                            '.jpg', '.gif', '.css', '.js']):
            filtered.append(e)
    return list(set(filtered))

def extract_phones(text):
    """Extract phone numbers from text."""
    patterns = [
        r'\+?\d{1,3}[-.\s]?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}',
        r'\(\d{3}\)\s*\d{3}[-.\s]?\d{4}',
        r'\d{10,12}',
    ]
    phones = []
    for pattern in patterns:
        found = re.findall(pattern, text)
        for p in found:
            cleaned = re.sub(r'[^\d+]', '', p)
            if 7 <= len(cleaned) <= 15:
                phones.append(p.strip())
    return list(set(phones))[:3]  # Max 3 phone numbers

def search_google(query, num_results=10):
    """Search Google and return result URLs and snippets."""
    results = []
    try:
        search_url = f"https://www.google.com/search?q={quote_plus(query)}&num={num_results}"
        resp = requests.get(search_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        for g in soup.select('div.g, div.tF2Cxc'):
            link_tag = g.select_one('a[href]')
            title_tag = g.select_one('h3')
            snippet_tag = g.select_one('div.VwiC3b, span.aCOpRe, div.IsZvec')
            
            if link_tag and title_tag:
                url = link_tag.get('href', '')
                if url.startswith('/url?q='):
                    url = url.split('/url?q=')[1].split('&')[0]
                if url.startswith('http') and 'google.com' not in url:
                    results.append({
                        'title': title_tag.get_text(strip=True),
                        'url': url,
                        'snippet': snippet_tag.get_text(strip=True) if snippet_tag else ''
                    })
    except Exception as e:
        logger.error(f"Google search error: {e}")
    
    return results[:num_results]

def search_bing(query, num_results=10):
    """Search Bing as fallback and return results."""
    results = []
    try:
        search_url = f"https://www.bing.com/search?q={quote_plus(query)}&count={num_results}"
        resp = requests.get(search_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        for li in soup.select('li.b_algo'):
            link_tag = li.select_one('h2 a')
            snippet_tag = li.select_one('div.b_caption p')
            
            if link_tag:
                url = link_tag.get('href', '')
                if url.startswith('http'):
                    results.append({
                        'title': link_tag.get_text(strip=True),
                        'url': url,
                        'snippet': snippet_tag.get_text(strip=True) if snippet_tag else ''
                    })
    except Exception as e:
        logger.error(f"Bing search error: {e}")
    
    return results[:num_results]

def scrape_website_for_contact(url):
    """Visit a website and extract contact information."""
    contact_info = {
        'emails': [],
        'phones': [],
        'website': url
    }
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        resp.raise_for_status()
        text = resp.text
        
        # Extract from main page
        contact_info['emails'] = extract_emails(text)
        contact_info['phones'] = extract_phones(text)
        
        # If no email found, try contact/about pages
        if not contact_info['emails']:
            soup = BeautifulSoup(text, 'html.parser')
            contact_links = []
            for a in soup.find_all('a', href=True):
                href = a['href'].lower()
                link_text = a.get_text(strip=True).lower()
                if any(word in href or word in link_text for word in ['contact', 'about', 'reach-us', 'get-in-touch']):
                    full_url = href
                    if href.startswith('/'):
                        from urllib.parse import urljoin
                        full_url = urljoin(url, href)
                    elif not href.startswith('http'):
                        continue
                    contact_links.append(full_url)
            
            # Visit up to 2 contact pages
            for contact_url in contact_links[:2]:
                try:
                    time.sleep(0.5)
                    resp2 = requests.get(contact_url, headers=HEADERS, timeout=8)
                    if resp2.ok:
                        contact_info['emails'].extend(extract_emails(resp2.text))
                        if not contact_info['phones']:
                            contact_info['phones'].extend(extract_phones(resp2.text))
                except:
                    pass
        
        # Deduplicate
        contact_info['emails'] = list(set(contact_info['emails']))[:3]
        contact_info['phones'] = list(set(contact_info['phones']))[:3]
        
    except Exception as e:
        logger.debug(f"Error scraping {url}: {e}")
    
    return contact_info

def find_business_leads(niche: str, location: str, max_results: int = 10) -> list:
    """
    Find business leads for a given niche and location.
    Returns a list of lead dictionaries.
    """
    leads = []
    seen_domains = set()
    
    # Build search queries
    queries = [
        f"{niche} in {location} contact email",
        f"{niche} {location} business directory",
        f"best {niche} in {location}",
    ]
    
    all_search_results = []
    
    # Try Google first, then Bing as fallback
    for query in queries[:2]:
        results = search_google(query, num_results=8)
        if not results:
            results = search_bing(query, num_results=8)
        all_search_results.extend(results)
        time.sleep(random.uniform(1, 2))
    
    # Process search results
    for result in all_search_results:
        if len(leads) >= max_results:
            break
        
        url = result['url']
        
        # Skip social media, directories listing pages, and duplicates
        skip_domains = ['facebook.com', 'twitter.com', 'instagram.com', 'linkedin.com',
                       'youtube.com', 'wikipedia.org', 'yelp.com', 'tripadvisor.com',
                       'google.com', 'bing.com', 'amazon.com', 'pinterest.com']
        
        domain = url.split('/')[2] if len(url.split('/')) > 2 else ''
        base_domain = '.'.join(domain.split('.')[-2:])
        
        if any(skip in domain for skip in skip_domains):
            continue
        if base_domain in seen_domains:
            continue
        seen_domains.add(base_domain)
        
        # Scrape the website for contact info
        contact = scrape_website_for_contact(url)
        
        # Extract emails and phones from search snippet too
        snippet_emails = extract_emails(result.get('snippet', ''))
        snippet_phones = extract_phones(result.get('snippet', ''))
        
        all_emails = list(set(contact['emails'] + snippet_emails))[:2]
        all_phones = list(set(contact['phones'] + snippet_phones))[:2]
        
        lead = {
            'business_name': result['title'],
            'website': url,
            'emails': all_emails,
            'phones': all_phones,
            'snippet': result.get('snippet', '')[:150]
        }
        
        leads.append(lead)
        time.sleep(random.uniform(0.5, 1.5))
    
    return leads

def format_leads_message(leads: list, niche: str, location: str) -> str:
    """Format leads into a nice Telegram message."""
    if not leads:
        return (
            f"😔 No leads found for *{niche}* in *{location}*.\n\n"
            "Try:\n"
            "• A broader niche (e.g., 'restaurants' instead of 'vegan restaurants')\n"
            "• A larger location (e.g., city name instead of neighborhood)\n"
            "• Different keywords"
        )
    
    msg = f"🔍 *Business Leads: {niche} in {location}*\n"
    msg += f"Found {len(leads)} leads:\n"
    msg += "━" * 30 + "\n\n"
    
    for i, lead in enumerate(leads, 1):
        msg += f"*{i}. {lead['business_name']}*\n"
        msg += f"🌐 {lead['website']}\n"
        
        if lead['emails']:
            msg += f"📧 {', '.join(lead['emails'])}\n"
        else:
            msg += "📧 Not found\n"
        
        if lead['phones']:
            msg += f"📞 {', '.join(lead['phones'])}\n"
        else:
            msg += "📞 Not found\n"
        
        if lead.get('snippet'):
            msg += f"📝 _{lead['snippet'][:100]}_\n"
        
        msg += "\n"
    
    msg += "━" * 30 + "\n"
    msg += "💡 *Tip:* Use /addlead to save leads you like, then /campaign to email them!"
    
    return msg
