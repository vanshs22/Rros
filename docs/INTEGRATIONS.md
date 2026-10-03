# Revenue Recovery OS — Integration Hub

v2.1 adds a tenant-scoped integration hub. Providers are represented by adapters rather than hard-coded business logic.

## Provider coverage

CRM: HubSpot, GoHighLevel, Salesforce, Pipedrive, Zoho CRM, Dynamics 365, Freshsales, Close, Keap.
Reviews: Google Business Profile, Yelp.
Communications: Gmail, Microsoft 365/Outlook, Twilio SMS/Voice, WhatsApp via Twilio.
Calendar: Google Calendar, Microsoft Graph calendar, Calendly, GoHighLevel calendar.
Payments/accounting: Stripe, Square, QuickBooks Online, Xero.
Field service: ServiceTitan, Housecall Pro, Jobber, JobNimbus, FieldEdge, AccuLynx, GorillaDesk.
Commerce: Shopify, WooCommerce.
Collaboration/support: Slack, Microsoft Teams, Zendesk, Intercom.

## Security model

- Every integration row belongs to an organization.
- Credentials are encrypted at rest before persistence.
- OAuth state is single-use, tenant-scoped and expires after 10 minutes.
- Provider access tokens are never returned by API responses.
- Disconnect deletes the stored credential ciphertext.
- Provider webhooks must be verified by their provider-specific signature adapter before being treated as trusted events.
- Sync/event records are tenant scoped and externally idempotent.

## Real-world setup

The application contains real provider endpoints and OAuth flows, but the operator still has to create developer applications with each provider and supply the resulting client credentials. Some providers require account approval, partner access, store-specific URLs, app keys, or scopes. The software does not fabricate those credentials.

Google Business Profile review APIs require an OAuth app and business.manage scope. HighLevel uses OAuth authorization and webhook subscriptions. Microsoft Graph exposes mail/calendar APIs. Twilio uses REST APIs plus webhooks for inbound messaging. ServiceTitan requires its developer/integration environment and OAuth credentials.
