import os
import re
import smtplib
from email.message import EmailMessage
from imap_tools import MailBox, A, AND, OR, NOT
import time

gmail_user = os.getenv('GMAIL_PRIMARY_ACCOUNT')
gmail_password = os.getenv('GMAIL_PRIMARY_ACCOUNT_APP_PASSWORD')
sent_from = 'Matthew Yglesias <matthewyglesias@substack.com>'
to = os.getenv('GMAIL_PODCAST_ACCOUNT')
start = False

server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
server.login(gmail_user, gmail_password)

# get list of email subjects from INBOX folder
with MailBox('imap.gmail.com').login(gmail_user, gmail_password) as mailbox:
    msgs = mailbox.fetch(AND(from_='matthewyglesias@substack.com'), mark_seen=False)
    for msg in msgs:
        if msg.subject[0:12] == 'Lawyer-brain':
            start = True
        if len(msg.text) != 0 and start and 'thread' not in msg.subject and 'Thread' not in msg.subject:
            print(f'sending {msg.subject}')
            newMsg = EmailMessage()
            newMsg.set_content(msg.text)
            clean_subject = re.sub(r'[^A-Za-z0-9 ]+', '', msg.subject)
            newMsg['Subject'] = clean_subject
            newMsg['From'] = sent_from
            newMsg['To'] = to
            server.send_message(newMsg)
            print('Email sent!')
            time.sleep(10)

server.close()
