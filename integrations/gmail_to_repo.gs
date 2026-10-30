// Google Apps Script: copies job-alert emails (WayUp, Handshake, LinkedIn) from your Gmail
// into data/inbox/ of the job-applier repo, where the next run turns them into job leads.
// Each email it copies is marked as read. It never visits the job sites themselves.
//
// Setup (once, ~5 minutes), in the Google account that receives the alerts:
//   1. script.google.com → New project → paste this file over the default code → Save.
//   2. Project Settings (gear) → Script properties → Add property:
//        GITHUB_TOKEN = a fine-grained GitHub token for this repo only, with
//                       "Contents: Read and write" permission.
//   3. Editor → select the `install` function → Run → approve the permissions prompt.
//      That syncs the last week of alerts now and then every 2 hours automatically, and checks
//      for approved verification codes and account-verification emails every minute
//      (see relayApprovedCodes and relayAccountVerifications below). It also marks "thank you for
//      applying" receipts as read every minute (markReceiptsRead).
//   After pasting a newer version of this file, run `install` again to refresh the triggers.

const REPO = 'troy-h-tian/job-applier';
const QUERY =
  'from:(wayup.com OR joinhandshake.com OR jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com) newer_than:7d';
const MAX_BODY_CHARS = 20000;

function syncJobEmails() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('Add the GITHUB_TOKEN script property first (Project Settings → Script properties).');

  // Track synced message ids rather than labelling threads: Gmail groups alerts with the same
  // subject into one thread, and new messages in a labelled thread would otherwise be missed.
  const synced = new Set(JSON.parse(props.getProperty('SYNCED_IDS') || '[]'));
  let added = 0;

  for (const thread of GmailApp.search(QUERY, 0, 100)) {
    for (const msg of thread.getMessages()) {
      const id = msg.getId();
      if (synced.has(id)) continue;
      const text = [
        'From: ' + msg.getFrom(),
        'Date: ' + msg.getDate().toISOString(),
        'Subject: ' + msg.getSubject(),
        '',
        msg.getPlainBody().slice(0, MAX_BODY_CHARS),
        '',
        // The plain-text body drops link targets, so list them from the HTML.
        'Links:',
        links_(msg.getBody()),
      ].join('\n');
      const path = 'data/inbox/' + msg.getDate().toISOString().slice(0, 10) + '-' + id + '.txt';
      const res = UrlFetchApp.fetch('https://api.github.com/repos/' + REPO + '/contents/' + path, {
        method: 'put',
        contentType: 'application/json',
        headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
        payload: JSON.stringify({
          message: 'Add job alert email ' + id,
          content: Utilities.base64Encode(text, Utilities.Charset.UTF_8),
        }),
        muteHttpExceptions: true,
      });
      const code = res.getResponseCode();
      // 422 means the file already exists, i.e. it was synced before.
      if (code >= 300 && code !== 422) throw new Error('GitHub API ' + code + ': ' + res.getContentText());
      msg.markRead();
      synced.add(id);
      added++;
    }
  }
  // Keep the newest 1000 ids; older messages fall outside the 7-day search window anyway.
  props.setProperty('SYNCED_IDS', JSON.stringify(Array.from(synced).slice(-1000)));
  console.log('Synced ' + added + ' new job alert email(s).');
}

// "- link text -> url" for each link in an HTML email, skipping unsubscribe/settings/footer links.
function links_(html) {
  const out = [];
  const seen = new Set();
  const re = /<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = re.exec(html)) && out.length < 80) {
    const url = m[1].replace(/&amp;/g, '&').trim();
    const text = m[2].replace(/<[^>]+>/g, ' ').replace(/&nbsp;|&#160;/g, ' ').replace(/\s+/g, ' ').trim();
    if (!/^https?:/i.test(url) || seen.has(url)) continue;
    if (/unsubscribe|preferences|privacy|terms|help center|view in browser|manage/i.test(text)) continue;
    seen.add(url);
    out.push('- ' + (text || '(no text)').slice(0, 150) + ' -> ' + url);
  }
  return out.join('\n');
}

// ---------------------------------------------------------------------------------------------
// Approved verification codes. When an application site emails you a security/verification code
// mid-application, reply "yes" to that email to approve that one submission. This relays only
// approved code emails to data/codes/ so the waiting agent can enter the code. Code emails you
// haven't replied "yes" to are never sent anywhere.

const CODE_QUERY =
  'newer_than:1d ("security code" OR "verification code" OR "one-time code" OR "one time code" OR passcode OR "confirmation code")';
const CODE_MAX_AGE_MINUTES = 30;

function relayApprovedCodes() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('Add the GITHUB_TOKEN script property first (Project Settings → Script properties).');
  // Your addresses: the account's own plus send-as aliases. Each code email's recipients are added
  // below too, since Gmail replies from whichever address a message was sent to (e.g. a
  // university alias like name@school.edu on a name@g.school.edu account).
  const mine = new Set([Session.getEffectiveUser().getEmail()].concat(GmailApp.getAliases()).map(addr_));
  const relayed = new Set(JSON.parse(props.getProperty('RELAYED_CODE_IDS') || '[]'));
  const cutoff = Date.now() - CODE_MAX_AGE_MINUTES * 60 * 1000;

  for (const thread of GmailApp.search(CODE_QUERY, 0, 20)) {
    const recent = thread.getMessages().filter((m) => m.getDate().getTime() > cutoff);
    for (const codeMsg of recent) {
      if (relayed.has(codeMsg.getId()) || mine.has(addr_(codeMsg.getFrom()))) continue;
      const me = new Set(mine);
      codeMsg.getTo().split(',').forEach((a) => a.trim() && me.add(addr_(a)));
      if (me.has(addr_(codeMsg.getFrom()))) continue; // one of your own replies, not a code email
      const repliedYes = recent.some(
        (m) => m.getDate() > codeMsg.getDate() && me.has(addr_(m.getFrom())) && /^\s*yes\b/i.test(m.getPlainBody())
      );
      if (!repliedYes) continue;
      relayCode_(token, codeMsg);
      relayed.add(codeMsg.getId());
    }
  }
  props.setProperty('RELAYED_CODE_IDS', JSON.stringify(Array.from(relayed).slice(-200)));
}

// "Jane Doe <JDoe+tag@School.edu>" -> "jdoe@school.edu". The +tag is dropped because Gmail delivers
// name+anything@ to name@, so replies sent from a +tag address (e.g. a canned-response
// template) are still yours.
function addr_(s) {
  const m = String(s).match(/<([^>]+)>/);
  return (m ? m[1] : String(s)).trim().toLowerCase().replace(/\+[^@]*@/, '@');
}

function relayCode_(token, codeMsg) {
  const text = [
    'From: ' + codeMsg.getFrom(),
    'Date: ' + codeMsg.getDate().toISOString(),
    'Subject: ' + codeMsg.getSubject(),
    'Approved: candidate replied "yes"',
    '',
    codeMsg.getPlainBody().slice(0, 5000),
  ].join('\n');
  putFile_(token, 'data/codes/' + codeMsg.getId() + '.txt', text, 'Relay approved verification code');
  codeMsg.markRead();
}

function putFile_(token, path, text, message) {
  const res = UrlFetchApp.fetch('https://api.github.com/repos/' + REPO + '/contents/' + path, {
    method: 'put',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json' },
    payload: JSON.stringify({ message: message, content: Utilities.base64Encode(text, Utilities.Charset.UTF_8) }),
    muteHttpExceptions: true,
  });
  const code = res.getResponseCode();
  if (code >= 300 && code !== 422) throw new Error('GitHub API ' + code + ': ' + res.getContentText());
}

// ---------------------------------------------------------------------------------------------
// Account verification. After the agent creates a candidate account on an application site
// (Workday, iCIMS, Taleo, a company's own portal, ...), many sites email a "verify your account"
// link before the application can continue. These are relayed to data/verify/ automatically (no
// reply needed). Senders vary by company, so the sender isn't checked; the email must look like a
// job-site account email, and the agent only opens a link on the site it is applying on
// (scripts/codes.py).

// Gmail search skips Spam and Trash unless told otherwise, and verification emails from unfamiliar
// senders often land in Spam, so search everywhere.
const VERIFY_QUERY =
  'in:anywhere newer_than:2d subject:(verify OR verification OR activate OR activation OR confirm) ' +
  '(workday OR myworkdayjobs OR myworkday OR candidate OR applicant OR application OR career OR careers OR talent OR job)';

function relayAccountVerifications() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('Add the GITHUB_TOKEN script property first (Project Settings → Script properties).');
  const relayed = new Set(JSON.parse(props.getProperty('RELAYED_VERIFY_IDS') || '[]'));

  // The log lines (Apps Script → Executions → the run) show what the relay saw, to diagnose problems.
  const threads = GmailApp.search(VERIFY_QUERY, 0, 30);
  console.log('Verification search found ' + threads.length + ' thread(s)');
  for (const thread of threads) {
    for (const msg of thread.getMessages()) {
      if (relayed.has(msg.getId())) continue;
      const links = allLinks_(msg).filter((url) => /^https:/i.test(url)).slice(0, 20);
      console.log('Verification email: ' + msg.getFrom() + ' | ' + msg.getSubject() + ' | ' + links.length + ' link(s)');
      if (!links.length) continue;
      const text = [
        'From: ' + msg.getFrom(),
        'Date: ' + msg.getDate().toISOString(),
        'Subject: ' + msg.getSubject(),
        '',
        'Links:',
      ].concat(links).join('\n');
      putFile_(token, 'data/verify/' + msg.getId() + '.txt', text, 'Relay account verification');
      msg.markRead();
      relayed.add(msg.getId());
    }
  }
  props.setProperty('RELAYED_VERIFY_IDS', JSON.stringify(Array.from(relayed).slice(-200)));
}

// Every https address in an email's HTML and plain text, as clickable links or written out. Some
// Workday emails show the verify link only as text.
function allLinks_(msg) {
  const out = [];
  const re = /https?:\/\/[^\s"'<>\\]+/gi;
  for (const body of [msg.getBody(), msg.getPlainBody()]) {
    let m;
    while ((m = re.exec(body)) && out.length < 40) {
      const url = m[0].replace(/&amp;/g, '&').replace(/[.,;:)\]]+$/, '');
      if (out.indexOf(url) < 0) out.push(url);
    }
  }
  return out;
}

// ---------------------------------------------------------------------------------------------
// Application receipts. "Thank you for applying" emails pile up as unread while the agent works,
// so they're marked read as they arrive. Anything that looks like an interview, assessment or
// other next step is left unread for you. (Codes: see markStaleCodesRead below.)

const RECEIPT_QUERY =
  'is:unread in:inbox newer_than:2d ("thank you for applying" OR "thanks for applying" OR ' +
  '"thank you for your application" OR "thanks for your application" OR ' +
  '"we received your application" OR "we have received your application" OR ' +
  '"application received" OR "application submitted" OR "your application to" OR ' +
  '"thank you for your interest")';
const NEXT_STEP = new RegExp(
  'interview|assessment|coding challenge|online test|hackerrank|codility|codesignal|schedule|' +
  'availability|phone screen|invit(e|ation)|next steps?|offer|congratulations|' +
  'verification code|security code|verify your|one-time|action required', 'i');

function markReceiptsRead() {
  for (const thread of GmailApp.search(RECEIPT_QUERY, 0, 50)) {
    for (const msg of thread.getMessages()) {
      if (!msg.isUnread()) continue;
      const text = msg.getSubject() + ' ' + msg.getPlainBody().slice(0, 3000);
      if (!/(thank|thanks)[^.]{0,30}appl|received your application|application (received|submitted)/i
          .test(text) || NEXT_STEP.test(text)) continue;
      msg.markRead();
    }
  }
  markStaleCodesRead();
}

// Security-code and "verify your ..." emails stay unread for CODE_MAX_AGE_MINUTES so you notice
// them (a code is approved by replying "yes" to its email), then are marked read.
const STALE_CODE_QUERY =
  'is:unread in:inbox newer_than:2d ("security code" OR "verification code" OR "one-time code" OR ' +
  '"one time code" OR passcode OR "confirmation code" OR "verify your")';

function markStaleCodesRead() {
  const cutoff = Date.now() - CODE_MAX_AGE_MINUTES * 60 * 1000;
  for (const thread of GmailApp.search(STALE_CODE_QUERY, 0, 50)) {
    for (const msg of thread.getMessages()) {
      if (msg.isUnread() && msg.getDate().getTime() < cutoff) msg.markRead();
    }
  }
}

function install() {
  PropertiesService.getScriptProperties().deleteProperty('RELAYED_VERIFY_IDS'); // retry emails skipped by older versions
  ScriptApp.getProjectTriggers().forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('syncJobEmails').timeBased().everyHours(2).create();
  ScriptApp.newTrigger('relayApprovedCodes').timeBased().everyMinutes(1).create();
  ScriptApp.newTrigger('relayAccountVerifications').timeBased().everyMinutes(1).create();
  ScriptApp.newTrigger('markReceiptsRead').timeBased().everyMinutes(1).create();
  syncJobEmails();
}
