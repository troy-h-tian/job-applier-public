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
//      for approved verification codes and Workday account-verification emails every minute
//      (see relayApprovedCodes and relayWorkdayVerifications below).
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
// Workday account verification. After the agent creates a Workday candidate account, some sites
// email a "verify your candidate account" link before the application can continue. These are
// relayed to data/verify/ automatically (no reply needed), but only the email's links that point
// at Workday itself. Senders vary by company (Workday@paloaltonetworks.com,
// motorolasolutions@otp.workday.com, ...@myworkday.com), so the sender isn't checked: only a link
// on a Workday host is relayed, and the agent only opens one from the site it is applying on.

const VERIFY_QUERY =
  'newer_than:1d {from:workday from:myworkday.com myworkdayjobs myworkday} {verify verification activate}';
const WORKDAY_HOST = /^https:\/\/[a-z0-9-]+(\.[a-z0-9-]+)*\.(myworkdayjobs|myworkday)\.com(\/|$)/i;

function relayWorkdayVerifications() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('Add the GITHUB_TOKEN script property first (Project Settings → Script properties).');
  const relayed = new Set(JSON.parse(props.getProperty('RELAYED_VERIFY_IDS') || '[]'));

  for (const thread of GmailApp.search(VERIFY_QUERY, 0, 30)) {
    for (const msg of thread.getMessages()) {
      if (relayed.has(msg.getId())) continue;
      if (!/verif|activat/i.test(msg.getSubject() + ' ' + msg.getPlainBody().slice(0, 2000))) continue;
      const links = workdayLinks_(msg.getBody());
      if (links.length) {
        const text = [
          'From: ' + msg.getFrom(),
          'Date: ' + msg.getDate().toISOString(),
          'Subject: ' + msg.getSubject(),
          '',
          'Links:',
        ].concat(links).join('\n');
        putFile_(token, 'data/verify/' + msg.getId() + '.txt', text, 'Relay Workday account verification');
        msg.markRead();
      }
      relayed.add(msg.getId());
    }
  }
  props.setProperty('RELAYED_VERIFY_IDS', JSON.stringify(Array.from(relayed).slice(-200)));
}

// Every distinct https link in an HTML email whose host is on myworkdayjobs.com / myworkday.com.
function workdayLinks_(html) {
  const out = [];
  const re = /<a\b[^>]*href\s*=\s*["']([^"']+)["']/gi;
  let m;
  while ((m = re.exec(html)) && out.length < 20) {
    const url = m[1].replace(/&amp;/g, '&').trim();
    if (WORKDAY_HOST.test(url) && out.indexOf(url) < 0) out.push(url);
  }
  return out;
}

function install() {
  ScriptApp.getProjectTriggers().forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('syncJobEmails').timeBased().everyHours(2).create();
  ScriptApp.newTrigger('relayApprovedCodes').timeBased().everyMinutes(1).create();
  ScriptApp.newTrigger('relayWorkdayVerifications').timeBased().everyMinutes(1).create();
  syncJobEmails();
}
