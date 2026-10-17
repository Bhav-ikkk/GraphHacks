import re
import subprocess

with open('web/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

current_ids = set(re.findall(r'id=["\']([^"\']+)["\']', html))
print(f"Current IDs count: {len(current_ids)}")

# Get original index.html from git commit HEAD (or root commit before refactor)
try:
    orig_html = subprocess.check_output(['git', 'show', 'HEAD:web/index.html'], text=True, encoding='utf-8')
    orig_ids = set(re.findall(r'id=["\']([^"\']+)["\']', orig_html))
    print(f"Original IDs count: {len(orig_ids)}")
    missing = orig_ids - current_ids
    print(f"Missing from original: {missing}")
except Exception as e:
    print(f"Error checking git: {e}")

# Check what IDs app.js references via $(...) or getElementById
with open('web/app.js', 'r', encoding='utf-8') as f:
    js = f.read()

js_dollar_ids = set(re.findall(r'\$\(["\']([^"\']+)["\']\)', js))
js_get_ids = set(re.findall(r'getElementById\(["\']([^"\']+)["\']\)', js))
all_js_ids = js_dollar_ids | js_get_ids
print(f"IDs accessed in app.js: {len(all_js_ids)}")
missing_in_html = all_js_ids - current_ids
print(f"IDs used in app.js but missing in index.html: {missing_in_html}")
