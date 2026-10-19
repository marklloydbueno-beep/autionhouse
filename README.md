# GoldenHammer Auction House — expanded marketplace starter

Responsive Flask marketplace with SQLite persistence, user accounts, separate administrator sign-in, product listings, bidding, auction history, seller profiles, watchlists, notifications, reports/moderation, value estimates, password reset/email verification flows, order/dispute records, and a local SQLite backup endpoint.

## Run locally in PyCharm (Windows)
1. Extract the ZIP and open the `GoldenHammer_AI` folder.
2. Use Python 3.11 or 3.12 and create a virtual environment.
3. In the PyCharm terminal run:
   ```powershell
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```
4. Set a strong `SECRET_KEY` environment variable before deployment. Set `ADMIN_EMAIL` and `ADMIN_PASSWORD` for the first administrator account. The default credentials are only for local demo use.
5. Run `app.py` and open `http://127.0.0.1:5000`.

## Main features
- Register/login with hashed passwords; separate admin login can be opened by holding the GoldenHammer logo for 5 seconds.
- Seller product listings, seller profile, editable products before bids, optional image uploads, category/search/price filters, and watchlists.
- Minimum bid increment, validation against current bids, prevention of self-bidding, full bid history, deadline closure on incoming requests, and winner status.
- Notifications for outbid users and ended auctions; account-scoped notification list.
- Email verification and password reset token flows. Email verification is OFF by default so local registration/login works without SMTP. To require verification for a public deployment, configure SMTP and set `REQUIRE_EMAIL_VERIFICATION=1`. Password reset email still requires SMTP.
- Rate limiting of repeated failed regular-login attempts, CSRF protection, and server-side role/ownership checks.
- Product value estimator uses category/condition/age/brand details and displays a low-confidence illustrative range. It does **not** access verified completed-sale data and is not a true market appraisal.
- Reporting and admin report review, local SQLite backup download, order records, and dispute intake.
- Terms, privacy, and prohibited-items starter pages.

## Email configuration
For email delivery, configure `SMTP_HOST`, `SMTP_PORT` (usually `587`), `SMTP_USER`, `SMTP_PASSWORD`, and `SMTP_FROM` in the hosting environment. Do not commit SMTP credentials. By default, email verification is disabled so you can register and log in locally without receiving an email. Enable it only after SMTP works by setting `REQUIRE_EMAIL_VERIFICATION=1`. Password reset emails require SMTP.

## Production deployment requirements
This project is a feature-rich starter, **not yet a fully audited commercial auction service**. Checkout currently creates a pending order record only; no card/payment is processed. Connect a payment provider such as Stripe or a locally supported provider using its official server-side SDK, webhook signature verification, idempotency, and test mode before real payments. Never collect card numbers directly in these forms.

For a public deployment:
- Use PostgreSQL or another managed persistent database; the included app uses SQLite and its backup route is intended for a single-instance/local deployment.
- Configure durable private file storage for uploads and database backups.
- Set a strong random `SECRET_KEY`, `COOKIE_SECURE=1`, HTTPS, `FLASK_DEBUG=0`, strong admin credentials, SMTP, database backups, monitoring, and recovery procedures.
- Configure scheduled/background jobs for exact auction closure and notification delivery. Current automatic closure runs when a request reaches the app, so it is not a guaranteed real-time scheduler while the app is idle.
- Review email verification enforcement, password reset delivery, privacy/terms text, moderation rules, identity checks, local legal obligations, tax, consumer protection, and dispute/refund policy with qualified professionals before commercial use.
- Run automated tests and a security review before accepting real users or money.

## Admin
The admin account is created on first database initialization using `ADMIN_EMAIL` and `ADMIN_PASSWORD`. Existing admin accounts keep their stored credentials; change them securely rather than relying on the demo defaults.


## Messaging and live-auction additions

This version adds:
- People search by display name, public seller profiles, seller following, direct conversations, and user blocking/reporting.
- Persistent private messages stored in SQLite, with product links, unread counts, and automatic refresh every 2 seconds while a chat is open.
- Live auction panels that refresh current bid, unique bidder count, leader, bid history, and countdown automatically. The server closes expired auctions on requests and creates winner notifications.
- An administrator user-report queue at `/admin/user-reports`.

**Realtime implementation note:** these features currently use short-interval HTTP polling, not WebSockets. This is a simpler starter implementation and can create extra requests with many concurrent visitors. For production scale, move chat and bid broadcasts to Flask-SocketIO (or a managed realtime service), run auction closing in a dedicated scheduler/worker, and use PostgreSQL plus backups. The local SQLite version is for development/demo; don't use it as the only database for a high-traffic production auction.

Private messaging and bids are application features, not a guarantee of delivery/availability. Test two separate user accounts in two browsers before deployment. User reports are queued for admin review; this project does not automatically suspend accounts. No real payment is processed.


## Real-time messaging and auction formats (upgrade)

- **Private messages:** saved to SQLite; Flask-SocketIO emits message events to connected users. The browser also refreshes messages periodically as a fallback.
- **Auction room chat:** signed-in regular users can send saved messages in an auction's room. Socket.IO broadcasts new messages to connected room members; a periodic refresh is used as a fallback.
- **Scheduled auctions:** use the end date/time, current bid, bidder count, leader and bid history. The existing request-time expiry check closes expired auctions when the site receives requests. For production, configure a reliable background scheduler/worker so closure and notifications happen even when the site is idle.
- **Host-led auctions:** choose this format when creating a listing. The listing owner or an admin can start the live room, pause the host, or close the auction early. Bidding still follows the auction's configured end time unless the host closes it.
- **Socket.IO:** install dependencies with `python -m pip install -r requirements.txt`. Local execution uses Flask-SocketIO's threading mode. The templates load the Socket.IO browser client from a CDN; internet access is required for that script. If the CDN is unavailable, periodic HTTP refresh remains as a fallback.

### Local run

```bash
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`. For two-user tests, register two accounts in separate browsers or an incognito window. Use one account to send a private message or post a bid and the other account to check updates. To test host-led controls, create a hosted listing and use the same seller account to start/pause/close its room.

### Hosting requirements and limits

- Deploy with a host that supports persistent WebSocket connections for Socket.IO. If using Gunicorn, use an appropriate Socket.IO worker or a single-worker threading configuration; plain multi-worker Gunicorn without Socket.IO-aware configuration can break room broadcasts.
- For more than one app instance, use a Socket.IO message queue such as Redis so broadcasts reach users connected to different instances.
- SQLite is for local/demo use. Use persistent managed PostgreSQL and durable upload storage for a public launch.
- Configure `SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, email SMTP settings, HTTPS and secure cookies. Change demo administrator credentials before deployment.
- **Not verified in this environment:** Flask and Flask-SocketIO were not installed in the build environment, so I could only run Python syntax compilation; live browser, Socket.IO, multi-user, and deployment tests still need to be performed after installing requirements.


## Latest UI and assistant fix
- Signed-in users and administrators now share a left-side vertical navigation layout, with a mobile slide-out sidebar. Logged-out visitors keep the compact public navigation.
- Fixed the Auction AI request to include the page's CSRF token. The previous chat JavaScript sent a POST request without the required `X-CSRF-Token`, so Flask rejected it before the assistant could respond.
- The assistant is a local listing-search helper, not a general-purpose generative AI. It searches listings and answers basic bidding/value questions using the site's current data; it does not browse the web or provide verified appraisals.


### Search autocomplete
The homepage search box now shows suggestions while typing. Suggestions are generated from currently open GoldenHammer listings (title, brand, category, and description) plus matching built-in categories. Users can click a suggestion or use the arrow keys and Enter. This is a local database-backed feature; it does not predict arbitrary internet searches or query YouTube/Google.

## Global website customization
After signing in with an administrator account, open **Website customization** in the admin sidebar (or `/admin/site-settings`). Admins can publish the site name, tagline, theme preset, accent/background colors, announcement banner, featured-auction heading visibility, and a maintenance notice. These settings are stored in the `site_settings` SQLite table and are shared across admin and customer pages; they survive restarts as long as the database file is preserved. The restore button resets the settings to the defaults without deleting users, listings, or bids. The maintenance toggle currently displays a notice; it does not fully disable marketplace routes.
