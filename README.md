# Podcast transcription service

## Purpose

I created this in order to consume Substack subscriptions in the form of a podcast, because I'm much more effective at listening to podcasts than I am at reading emails.

## Explanation

The process.sh script is set up as a cron job. Every 20 minutes, it uses a Python script to  check the podcast-specific email account for new emails. If they exist, it downloads them locally.

The next Python script then checks the local directory for unprocessed emails. If they exist, it breaks the emails into chunks between 4000 and 5000 characters, stopping at the first whitespace to ensure the text-to-speech processing doesn't cut off individual words. The Google text-to-speech API has a limit of 5,000 characters at a time. Each chunk of the email is sent to Google for processing and an mp3 is returned in response. The mp3's are saved in order, and stitched together afterward. Once the stitching is confirmed successful, the file is moved to the podcast audio folder, and the email is removed from the local directory. There are safeguards for the maximum size of the email text to ensure that my Gooogle Cloud account isn't charged too much for any individual email, although a processing error of some kind could still cause issues since there isn't a full safeguard in place to shut down the account if too many characters are processed in a month. There is also a check to avoid processing empty emails. Some boilerplate Substack items are stripped out, as well as URL's, since they don't make for good podcast listening.

After this, a Docker container called dropcaster by nerab processes the audio files into a podcast feed. Every time this takes place, a hash is taken of the audio directory so that dropcaster only needs to run if the directory has changed.

The script then updates Google DNS with my current IP in case it has changed since last time.

Lastly, Docker cleanup is run since the dropcaster run leaves behind a new stopped container every single time.

There is also a second script that can run which uses the AWS Polly text-to-speech service. I have tried this out but I prefer the Google service. Others say that the Polly one is more natural-sounding. In order for the AWS script to work, AWS credentials need to be set at the ENV level on the machine running the script.

### Environment variables required

This is the list of ENV variables that need to be set locally for this script to work.

- GMAIL_PRIMARY_ACCOUNT

    Primary email of user, used for domain registration

- GMAIL_PODCAST_ACCOUNT

    Email account where all emails will be turned into podcast episodes

- GMAIL_PODCAST_ACCOUNT_APP_PASSWORD

    App password for podcast account

- GOOGLE_DOMAIN_1

    Primary domain for Google DNS

- GOOGLE_DOMAIN_1_KEY

    Username:password combo for setting Google DNS

- GOOGLE_DOMAIN_2

    Secondary domain for Google DNS

- GOOGLE_DOMAIN_2_KEY

    Username:password combo for setting Google DNS

- PODCAST_DOMAIN_PRIMARY

    Primary podcast domain

- PODCAST_DOMAIN_SECONDARY

    Secondary (AWS Polly) podcast domain
