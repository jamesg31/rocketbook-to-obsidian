from icloudpy import ICloudPyService
import sys
import sqlite3
from flask import Flask, request, Response, g, current_app
from imaplib import IMAP4_SSL
import email
import click
import threading
import time
import os
from datetime import date
import logging
import sys

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# create a formatter that will add the time to the log message
formatter = logging.Formatter(fmt="%(asctime)s %(name)s.%(levelname)s: %(message)s", datefmt="%Y.%m.%d %H:%M:%S")

# create a handler that will log to the console
handler = logging.StreamHandler(stream=sys.stdout)
handler.setFormatter(formatter)
logger.addHandler(handler)

IMAP_SERVER = os.environ.get('IMAP_SERVER')
IMAP_USER = os.environ.get('IMAP_USER')
IMAP_PASSWORD = os.environ.get('IMAP_PASSWORD')
DATABASE = 'rocketbook.db'
app = Flask(__name__)
api = ICloudPyService(os.environ.get('ICLOUD_USER'), os.environ.get('ICLOUD_PASSWORD'))

if api.requires_2fa:
    logger.info("Two-factor authentication required.")
    code = input("Enter the code you received of one of your approved devices: ")
    result = api.validate_2fa_code(code)
    logger.info("Code validation result: %s" % result)

    if not result:
        logger.info("Failed to verify security code")
        sys.exit(1)

    if not api.is_trusted_session:
        logger.info("Session is not trusted. Requesting trust...")
        result = api.trust_session()
        logger.info("Session trust result %s" % result)

        if not result:
            logger.info("Failed to request trust. You will likely be prompted for the code again in the coming weeks")
elif api.requires_2sa:
    import click
    logger.info("Two-step authentication required. Your trusted devices are:")

    devices = api.trusted_devices
    for i, device in enumerate(devices):
        logger.info(
            "  %s: %s" % (i, device.get('deviceName',
            "SMS to %s" % device.get('phoneNumber')))
        )

    device = click.prompt('Which device would you like to use?', default=0)
    device = devices[device]
    if not api.send_verification_code(device):
        logger.info("Failed to send verification code")
        sys.exit(1)

    code = click.prompt('Please enter validation code')
    if not api.validate_verification_code(device, code):
        logger.info("Failed to verify verification code")
        sys.exit(1)

def get_db():
    with app.app_context():
        if 'db' not in g:
            g.db = sqlite3.connect(
                DATABASE,
                detect_types=sqlite3.PARSE_DECLTYPES
            )
            g.db.row_factory = sqlite3.Row

        return g.db


def close_db(e=None):
    db = g.pop('db', None)

    if db is not None:
        db.close()

def init_db():
    db = get_db()

    with current_app.open_resource('schema.sql') as f:
        db.executescript(f.read().decode('utf8'))


@click.command('init-db')
def init_db_command():
    """Clear the existing data and create new tables."""
    init_db()
    click.echo('Initialized the database.')

class ImapConnection:
    def __init__(self, server, user, password):
        try:
            self.conn = IMAP4_SSL(server)
            self.conn.login(user, password)
        except:
            logger.error(sys.exc_info()[1])
            sys.exit(1)

        self.conn.select('Rocketbook', readonly=False)
    
    def get_messages(self, charset, criteria):
        messages = []
        (retcode, messages) = self.conn.search(charset, criteria)
        if retcode == 'OK':
            for num in messages[0].split():
                typ, data = self.conn.fetch(num, '(BODY.PEEK[])')
                messages.append({
                    'num': num,
                    'data': data
                })

        return messages
    
    def store(self, num, flags, flag):
        self.conn.store(num, flags, flag)
    
    def close(self):
        self.conn.close()

def process_messages():
    time.sleep(5)
    conn = ImapConnection(IMAP_SERVER, IMAP_USER, IMAP_PASSWORD)
    # search for unseen messages sent too james+rocketbook@gardna.net
    messages = conn.get_messages(None, '(UNSEEN TO james+rocketbook@gardna.net)')
    logger.info('Processing %s new messages' % len(messages))
    db = get_db()
    for message in messages:
        mail = email.message_from_string(message['data'][0][1])
        message_id = mail.get('Message-ID')
        # if message id is already in the database, skip it
        if db.execute('SELECT message_id FROM email WHERE message_id = ?', (message_id,)).fetchone() is not None:
            logger.info('Message ID %s is already in the database' % message_id)
            continue

        # insert message id into database
        db.execute('INSERT INTO email (message_id, processed) VALUES (?, FALSE)', (message_id,))
        db.commit()
        # download attachments
        if mail.get_content_maintype() != 'multipart':
            return
        logger.info('Downloading attachments for message ID %s' % message_id)
        try:
            os.makedirs(message_id, exist_ok=True)
        except:
            logger.error('Error creating directory for message ID %s' % message_id)
            continue
        for part in mail.walk():
            if part.get_content_maintype() != 'multipart' and part.get('Content-Disposition') is not None:
                open(message_id + '/' + part.get_filename(), 'wb').write(part.get_payload(decode=True))

        # get icloud (ubiquity) node for obsidian
        obsidian_node = api.drive.get_app_node('iCloud.md.obsidian')

        for attachment in os.listdir(message_id):
            # upload PDF
            if attachment.endswith('.pdf'):
                # rename pdf to include date
                pdf = attachment.split('[')[0] + str(date.today()) + '.pdf'
                os.rename(message_id + '/' + attachment, message_id + '/' + pdf)
                logger.info('Uploading pdf %s to iCloud' % pdf)
                with open(message_id + '/' + pdf, 'rb') as f:
                    obsidian_node['james']['rocketbook']['pdfs'].upload(f)
                os.remove(message_id + '/' + pdf)

        for attachment in os.listdir(message_id):
            # generate markdown file from .txt
            if attachment.endswith('.txt'):
                logger.info('Converting attachment %s to markdown' % attachment)
                with open(message_id + '/' + attachment, 'r') as f:
                    with open(message_id + '/' + attachment.split(' [')[0] + '.md', 'w') as md:
                        md.write('#%s\n\n' % attachment.split(' [')[1].split(']')[0])
                        md.write('![[%s]]\n\n' % pdf)
                        md.write(f.read())

        # upload markdown file
        logger.info('Uploading markdown file %s.md to iCloud' % attachment.split(' [')[0])
        with open(message_id + '/' + attachment.split(' [')[0] + '.md', 'rb') as f:
            obsidian_node['james']['rocketbook'].upload(f)
            os.remove(message_id + '/' + attachment)
            os.remove(message_id + '/' + attachment.split(' [')[0] + '.md')
        
        os.removedirs(message_id)
        conn.store(message['num'], '+FLAGS', '\\Seen')
        # mark message as read in the mailbox and update the database
        db.execute('UPDATE email SET processed = TRUE WHERE message_id = ?', (message_id,))
        db.commit()
    
    conn.close()


@app.route('/', methods=['POST'])
def respond():
    task = threading.Thread(target=process_messages, daemon=True)
    task.start()
    return Response(status=200)

app.cli.add_command(init_db_command)

if __name__ == '__main__':
    app.run(port=8000, debug=True)