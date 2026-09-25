# SecureLoginForUrlwatch
Encrypted sessions and browser capture for [urlwatch](https://github.com/thp/urlwatch).
Useful if you want to watch pages that require logging-in, e.g., the "Who's viewed your profile" page on LinkedIn.
Instead of manually exporting cookies for urlwatch to use, this script opens up a browser for you to login, and saves the session cookies securely.

Sample urlwatch job:
```yaml
name: LinkedInProfileViews
command: "python -X utf8 Playwright.py https://www.linkedin.com/analytics/profile-views/ --wait-for \"text=/^Viewed .+ ago$/\""
kind: shell
stderr: stdout
filter:
  - xpath: '//body//text()[normalize-space() and not(ancestor::script or ancestor::style or ancestor::noscript)]' # Extract page text
  - re.sub: '\[\]|[\u200b\u200e\u200f\ufeff]' # Remove empty brackets and invisible formatting characters
  - striplines
  - re.sub: '(?ms)\A.*?^Viewer details$\n?' # Remove everything before viewer list
  - re.sub: '(?ms)^(About|Ad Options)$.*\Z' # Remove the footer and everything after
  - grepi: '^\s*(?:View|Message|Connect|-+|Browse up to .*|[•·]|(?:[•·]\s*)?(?:1st|2nd|3rd\+?)|\d+ mutual connections?|Viewed .* ago)?\s*$'
  - remove-duplicate-lines
```
