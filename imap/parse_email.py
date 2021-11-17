import os
import re
import operator

from imap_tools import MailBox, A, AND, OR, NOT, MailMessageFlags

output_folder = '../text-to-speech/text-input'
gmail_user = os.getenv('GMAIL_PODCAST_ACCOUNT')
gmail_password = os.getenv('GMAIL_PODCAST_ACCOUNT_APP_PASSWORD')

# get list of email subjects from INBOX folder
with MailBox('imap.gmail.com').login(gmail_user, gmail_password) as mailbox:
    msgs = mailbox.fetch(AND(seen=False), mark_seen=False)
    for msg in msgs:
        subject = msg.subject.replace('Fwd: ','')
        date = msg.date.strftime('%Y%m%d-%H%M%S-%f')[0:15]
        from_ = msg.from_values.name
        clean_from = re.sub(r'[^A-Za-z0-9 ]+', '', from_)
        clean_from = clean_from + '- ' if clean_from != '' else ''
        clean_subject = re.sub(r'[^A-Za-z0-9 ]+', '', subject)
        output_filename = f'{output_folder}/{date}-{clean_from}{clean_subject}.txt'
        print(f'parsing email: {output_filename}')
        email_text = msg.text
        first_clean_email_text = re.sub(r'https?:\/\/(www\.)?[-a-zA-Z0-9@:%._\+~#=]{2,256}\.[a-z]{2,4}\b([-a-zA-Z0-9@:%_\+.~#?&//=]*)', '', email_text)
        second_clean_email_text = re.sub(r'\[\]', '', first_clean_email_text)
        third_clean_email_text = re.sub(r'\(\)', '', second_clean_email_text)
        clean_email_text = re.sub(r'<>', '', third_clean_email_text)
        output_file = open(output_filename,"w")
        output_file.write(clean_email_text)
        output_file.close()
        flags = (MailMessageFlags.SEEN)
        mailbox.flag(msg.uid, flags, True)
