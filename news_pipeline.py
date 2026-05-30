import json
import datetime
import os
import subprocess
from hermes_tools import terminal

def run_pipeline():
    today = datetime.date.today().isoint()
    digest_dir = os.path.expanduser("~/.hermes/bobclawblaw/digests")
    os.makedirs(digest_dir, exist_ok=True)
    md_path = os.path.join(digest_dir, f"{today}.md")
    bbcode_path = os.path.join(digest_dir, f"{today}.bbcode.txt")
    
    # 1. Search for links
    search_cmd = "curl -s -X POST http://localhost:3002/v1/search -H 'Content-Type: application/json' -d '{\"query\": \"bitcoin news today\", \"limit\": 15}'"
    search_res = terminal(search_cmd)
    if search_res['exit_code'] != 0:
        print("Search failed")
        return
    
    items = json.loads(search_res['output'])
    if isinstance(items, dict): items = items.get('data', [])
    
    stories = []
    for item in items[:10]:
        url = item.get('url')
        if not url: continue
        
        # 2. Scrape each link
        scrape_cmd = f"curl -s -X POST http://localhost:3002/v1/scrape -H 'Content-Type: application/json' -d '{{\"url\": \"{url}\"}}'"
        scrape_res = terminal(scrape_cmd)
        content = ""
        if scrape_res['exit_code'] == 0:
            try:
                scrape_data = json.loads(scrape_res['output'])
                content = scrape_data.get('content', '') or scrape_data.get('text', '')
            except:
                content = "Scrape failed."
        
        stories.append({
            'title': item.get('title', 'No Title'),
            'url': url,
            'text': content.replace('\\n', ' ').strip()[:500]
        })
    
    # 3. Construct Digest
    lines = [f"Title: BobClawblaw's Wall Observer Digest — {today}", "",
             "The markets are moving as usual, but there's enough noise today to warrant a look.", ""]
    lines.append("Key market movers:")
    for s in stories:
        lines.append(f"- **{s['title']}**")
        lines.append(f"  {s['url']}")
        lines.append(f"  {s['text']}...")
        lines.append("")
    
    lines.append("Outlook: Watching the macro environment closely.")
    lines.append("")
    lines.append("BobClawblaw")
    
    with open(md_path, 'w') as f:
        f.write("\n".join(lines))
        
    # 4. Sanitize
    sanitize_script = os.path.expanduser("~/.hermes/skills/wall-observer-bot/scripts/sanitize_bbcode.py")
    if os.path.exists(sanitize_script):
        terminal(f"python3 {sanitize_script} {md_path} {bbcode_path}")
    
    print(f"Pipeline Complete. Created {md_path}")

if __name__ == '__main__':
    run_pipeline()
