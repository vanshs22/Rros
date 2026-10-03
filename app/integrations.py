from dataclasses import dataclass, field
from typing import Any, Optional, Dict
from urllib.parse import urlencode
import hashlib, hmac, json, time, base64, ipaddress, socket
from urllib.parse import urlencode, urlparse, quote
import httpx

@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    category: str
    auth: str
    scopes: tuple[str, ...] = ()
    authorize_url: Optional[str] = None
    token_url: Optional[str] = None
    api_base: Optional[str] = None
    test_path: Optional[str] = None
    webhook: bool = True
    capabilities: tuple[str, ...] = ()
    notes: str = ''
    implementation: str = 'registry_only'


def _p(key,name,category,auth,**kw):
    return Provider(key,name,category,auth,**kw)

PROVIDERS = {
 'google_business_profile': _p('google_business_profile','Google Business Profile','reviews','oauth',authorize_url='https://accounts.google.com/o/oauth2/v2/auth',token_url='https://oauth2.googleapis.com/token',api_base='https://mybusiness.googleapis.com/v4',test_path='/accounts',scopes=('https://www.googleapis.com/auth/business.manage',),capabilities=('locations','reviews.read','reviews.reply')),
 'google_gmail': _p('google_gmail','Gmail','communications','oauth',authorize_url='https://accounts.google.com/o/oauth2/v2/auth',token_url='https://oauth2.googleapis.com/token',api_base='https://gmail.googleapis.com/gmail/v1',test_path='/users/me/profile',scopes=('https://www.googleapis.com/auth/gmail.modify',),capabilities=('email.read','email.send','threads')),
 'google_calendar': _p('google_calendar','Google Calendar','calendar','oauth',authorize_url='https://accounts.google.com/o/oauth2/v2/auth',token_url='https://oauth2.googleapis.com/token',api_base='https://www.googleapis.com/calendar/v3',test_path='/users/me/calendarList',scopes=('https://www.googleapis.com/auth/calendar',),capabilities=('events.read','events.write','freebusy')),
 'hubspot': _p('hubspot','HubSpot','crm','oauth',authorize_url='https://app.hubspot.com/oauth/authorize',token_url='https://api.hubapi.com/oauth/v1/token',api_base='https://api.hubapi.com',test_path='/crm/v3/objects/contacts?limit=1',scopes=('crm.objects.contacts.read','crm.objects.contacts.write','crm.objects.deals.read','crm.objects.deals.write','crm.objects.companies.read','crm.objects.companies.write','crm.objects.custom.read','crm.objects.custom.write'),capabilities=('contacts','companies','deals','activities','webhooks')),
 'gohighlevel': _p('gohighlevel','GoHighLevel','crm','oauth',authorize_url='https://marketplace.gohighlevel.com/oauth/chooselocation',token_url='https://services.leadconnectorhq.com/oauth/token',api_base='https://services.leadconnectorhq.com',test_path='/locations/search',scopes=('contacts.readonly','contacts.write','calendars.readonly','calendars.write','conversations.readonly','conversations.write','opportunities.readonly','opportunities.write','payments.readonly'),capabilities=('contacts','conversations','calendars','opportunities','payments','webhooks')),
 'microsoft_graph': _p('microsoft_graph','Microsoft 365 / Outlook','communications','oauth',authorize_url='https://login.microsoftonline.com/common/oauth2/v2.0/authorize',token_url='https://login.microsoftonline.com/common/oauth2/v2.0/token',api_base='https://graph.microsoft.com/v1.0',test_path='/me',scopes=('openid','profile','offline_access','User.Read','Mail.ReadWrite','Mail.Send','Calendars.ReadWrite','Contacts.Read'),capabilities=('mail','calendar','contacts','webhooks')),
 'salesforce': _p('salesforce','Salesforce','crm','oauth',authorize_url='https://login.salesforce.com/services/oauth2/authorize',token_url='https://login.salesforce.com/services/oauth2/token',api_base=None,test_path='/services/data/v60.0/sobjects/',scopes=('api','refresh_token','offline_access'),capabilities=('leads','contacts','accounts','opportunities','activities','webhooks')),
 'pipedrive': _p('pipedrive','Pipedrive','crm','oauth',authorize_url='https://oauth.pipedrive.com/oauth/authorize',token_url='https://oauth.pipedrive.com/oauth/token',api_base='https://api.pipedrive.com/v1',test_path='/users/me',scopes=(),capabilities=('persons','organizations','deals','activities','webhooks')),
 'zoho_crm': _p('zoho_crm','Zoho CRM','crm','oauth',authorize_url='https://accounts.zoho.com/oauth/v2/auth',token_url='https://accounts.zoho.com/oauth/v2/token',api_base='https://www.zohoapis.com/crm/v7',test_path='/users?type=CurrentUser',scopes=('ZohoCRM.modules.ALL','ZohoCRM.settings.ALL','offline_access'),capabilities=('leads','contacts','deals','activities','webhooks')),
 'stripe': _p('stripe','Stripe','payments','api_key',api_base='https://api.stripe.com/v1',test_path='/balance',webhook=True,capabilities=('customers','payments','invoices','subscriptions','refunds','webhooks')),
 'twilio': _p('twilio','Twilio SMS / Voice','communications','api_key',api_base='https://api.twilio.com/2010-04-01',test_path='/Accounts',capabilities=('sms','voice','delivery_status','webhooks')),
 'whatsapp_twilio': _p('whatsapp_twilio','WhatsApp via Twilio','communications','api_key',api_base='https://api.twilio.com/2010-04-01',test_path='/Accounts',capabilities=('whatsapp','templates','delivery_status','webhooks')),
 'quickbooks': _p('quickbooks','QuickBooks Online','accounting','oauth',authorize_url='https://appcenter.intuit.com/connect/oauth2',token_url='https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer',api_base='https://quickbooks.api.intuit.com/v3/company',test_path=None,scopes=('com.intuit.quickbooks.accounting',),capabilities=('customers','invoices','payments','webhooks')),
 'xero': _p('xero','Xero','accounting','oauth',authorize_url='https://login.xero.com/identity/connect/authorize',token_url='https://identity.xero.com/connect/token',api_base='https://api.xero.com/api.xro/2.0',test_path='/Organisation',scopes=('openid','profile','email','offline_access','accounting.transactions','accounting.contacts'),capabilities=('contacts','invoices','payments','webhooks')),
 'square': _p('square','Square','payments','oauth',authorize_url='https://connect.squareup.com/oauth2/authorize',token_url='https://connect.squareup.com/oauth2/token',api_base='https://connect.squareup.com/v2',test_path='/merchants/me',scopes=('MERCHANT_PROFILE_READ','PAYMENTS_READ','ORDERS_READ','CUSTOMERS_READ'),capabilities=('payments','orders','customers','webhooks')),
 'shopify': _p('shopify','Shopify','commerce','oauth',authorize_url=None,token_url=None,api_base=None,test_path='/admin/api/2026-07/shop.json',capabilities=('orders','customers','products','webhooks'),notes='Per-store OAuth; shop domain is supplied during connection.'),
 'woocommerce': _p('woocommerce','WooCommerce','commerce','api_key',api_base=None,test_path='/wp-json/wc/v3/system_status',capabilities=('orders','customers','products','webhooks'),notes='Per-store URL plus consumer key/secret.'),
 'calendly': _p('calendly','Calendly','calendar','oauth',authorize_url='https://auth.calendly.com/oauth/authorize',token_url='https://auth.calendly.com/oauth/token',api_base='https://api.calendly.com',test_path='/users/me',scopes=(),capabilities=('scheduled_events','invitees','webhooks')),
 'slack': _p('slack','Slack','collaboration','oauth',authorize_url='https://slack.com/oauth/v2/authorize',token_url='https://slack.com/api/oauth.v2.access',api_base='https://slack.com/api',test_path='/auth.test',scopes=('channels:read','chat:write','users:read'),capabilities=('messages','alerts','webhooks')),
 'microsoft_teams': _p('microsoft_teams','Microsoft Teams','collaboration','oauth',authorize_url='https://login.microsoftonline.com/common/oauth2/v2.0/authorize',token_url='https://login.microsoftonline.com/common/oauth2/v2.0/token',api_base='https://graph.microsoft.com/v1.0',test_path='/me',scopes=('openid','profile','offline_access','User.Read','ChannelMessage.Send'),capabilities=('notifications','messages','webhooks')),
 'servicetitan': _p('servicetitan','ServiceTitan','field_service','oauth',authorize_url=None,token_url='https://auth-integration.servicetitan.io/connect/token',api_base='https://api-integration.servicetitan.io',test_path='/settings/v2/tenant/{tenant_id}/employees?pageSize=1',capabilities=('customers','jobs','appointments','estimates','invoices','payments'),notes='Requires ServiceTitan developer/app approval and tenant/app-key configuration.'),
 'housecall_pro': _p('housecall_pro','Housecall Pro','field_service','api_key',api_base='https://api.housecallpro.com',test_path='/customers?page=1&page_size=1',capabilities=('customers','jobs','appointments','invoices','payments','webhooks')),
 'jobber': _p('jobber','Jobber','field_service','oauth',authorize_url='https://api.getjobber.com/api/oauth/authorize',token_url='https://api.getjobber.com/api/oauth/token',api_base='https://api.getjobber.com/api/graphql',test_path=None,scopes=(),capabilities=('clients','jobs','appointments','invoices','payments','webhooks')),
 'jobnimbus': _p('jobnimbus','JobNimbus','field_service','api_key',api_base='https://api.jobnimbus.com',test_path='/identity/v1/me/companies',capabilities=('contacts','jobs','estimates','tasks','webhooks')),
 'fieldedge': _p('fieldedge','FieldEdge','field_service','oauth',authorize_url=None,token_url='https://fieldedge.withgobo.com/oauth/token',api_base='https://api.fieldedge.com/open-api',test_path='/v1/business-units?limit=1',capabilities=('customers','leads','work_orders','appointments','invoices','payments'),notes='Partner-gated OAuth; requires FieldEdge Marketplace approval and tenant target_id.'),
 'acculynx': _p('acculynx','AccuLynx','field_service','api_key',api_base='https://api.acculynx.com',test_path='/api/v2/contacts?pageSize=1&pageStartIndex=0',capabilities=('companies','jobs','estimates','invoices','webhooks')),
 'gorilladesk': _p('gorilladesk','GorillaDesk','field_service','api_key',api_base='https://api.gorilladesk.com',test_path=None,capabilities=('customers','appointments','jobs','invoices'),notes='API endpoint/credentials depend on account plan.'),
 'dynamics_365': _p('dynamics_365','Microsoft Dynamics 365','crm','oauth',authorize_url='https://login.microsoftonline.com/common/oauth2/v2.0/authorize',token_url='https://login.microsoftonline.com/common/oauth2/v2.0/token',api_base=None,test_path='/api/data/v9.2/WhoAmI',scopes=('openid','profile','offline_access'),capabilities=('leads','contacts','accounts','opportunities','activities','webhooks')),
 'freshsales': _p('freshsales','Freshsales','crm','api_key',api_base=None,test_path='/api/contacts?per_page=1',capabilities=('contacts','deals','activities','webhooks'),notes='Account-specific domain is required.'),
 'close': _p('close','Close CRM','crm','api_key',api_base='https://api.close.com/api/v1',test_path='/me/',capabilities=('leads','contacts','opportunities','activities','webhooks')),
 'keap': _p('keap','Keap','crm','oauth',authorize_url='https://accounts.infusionsoft.com/app/oauth/authorize',token_url='https://api.infusionsoft.com/token',api_base='https://api.infusionsoft.com/crm/rest/v2',test_path='/account/profile',scopes=(),capabilities=('contacts','opportunities','orders','payments','appointments','webhooks')),
 'zendesk': _p('zendesk','Zendesk','support','api_key',api_base=None,test_path='/api/v2/users/me.json',capabilities=('tickets','users','organizations','webhooks'),notes='Subdomain-specific base URL is required.'),
 'intercom': _p('intercom','Intercom','support','api_key',api_base='https://api.intercom.io',test_path='/me',capabilities=('contacts','conversations','tickets','webhooks')),
 'yelp': _p('yelp','Yelp','reviews','api_key',api_base='https://api.yelp.com/v3',test_path='/businesses/search?term=business&location=San%20Francisco&limit=1',capabilities=('businesses','reviews.read')),
}


def public_catalog():
    from .connectors import ADAPTERS
    out = []
    for p in PROVIDERS.values():
        oauth_ready = p.auth == 'oauth' and bool(p.authorize_url and p.token_url)
        endpoint_ready = bool(p.api_base and p.test_path)
        if p.auth == 'oauth' and oauth_ready:
            connection_readiness = 'oauth_flow_ready'
        elif p.auth == 'oauth':
            connection_readiness = 'account_specific_setup'
        elif endpoint_ready:
            connection_readiness = 'api_test_ready'
        else:
            connection_readiness = 'account_specific_setup'
        out.append({
            'provider': p.key, 'name': p.name, 'category': p.category, 'auth': p.auth,
            'scopes': list(p.scopes), 'capabilities': list(p.capabilities), 'webhook': p.webhook,
            'notes': p.notes, 'implementation': 'live_adapter_installed' if p.key in ADAPTERS else p.implementation,
            'oauth_ready': oauth_ready, 'endpoint_ready': endpoint_ready,
            'connection_readiness': connection_readiness, 'live_adapter': p.key in ADAPTERS,
        })
    return out


def provider(key:str)->Provider:
    if key not in PROVIDERS: raise KeyError(key)
    return PROVIDERS[key]


def oauth_url(p:Provider, client_id:str, redirect_uri:str, state:str, extra:Optional[Dict[str,Any]]=None):
    if not p.authorize_url: raise ValueError(f'{p.name} does not use the generic OAuth authorization URL')
    params={'client_id':client_id,'redirect_uri':redirect_uri,'response_type':'code','state':state}
    if p.scopes: params['scope']=' '.join(p.scopes)
    if extra: params.update(extra)
    return p.authorize_url+'?'+urlencode(params)

async def exchange_code(p:Provider, client_id:str, client_secret:str, code:str, redirect_uri:str):
    if not p.token_url: raise ValueError('Provider token exchange is not configured')
    async with httpx.AsyncClient(timeout=20) as c:
        r=await c.post(p.token_url,data={'grant_type':'authorization_code','client_id':client_id,'client_secret':client_secret,'code':code,'redirect_uri':redirect_uri},headers={'Accept':'application/json'})
        r.raise_for_status(); return r.json()

async def refresh_token(p:Provider, client_id:str, client_secret:str, refresh:str):
    async with httpx.AsyncClient(timeout=20) as c:
        r=await c.post(p.token_url,data={'grant_type':'refresh_token','client_id':client_id,'client_secret':client_secret,'refresh_token':refresh},headers={'Accept':'application/json'})
        r.raise_for_status(); return r.json()

def validate_remote_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme != 'https':
        raise ValueError('Provider endpoints must use HTTPS')
    if not parsed.hostname:
        raise ValueError('Provider endpoint has no hostname')
    host = parsed.hostname.lower().rstrip('.')
    if host in {'localhost', 'localhost.localdomain'}:
        raise ValueError('Private/local endpoints are not allowed')
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = {info[4][0] for info in infos}
    except OSError as exc:
        raise ValueError('Provider endpoint hostname could not be resolved') from exc
    for addr in addresses:
        ip = ipaddress.ip_address(addr)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValueError('Provider endpoint resolves to a non-public IP address')
    return parsed


def _auth_headers(p: Provider, creds: dict[str, Any], token: str):
    if p.key in {'twilio', 'whatsapp_twilio'}:
        sid = creds.get('account_sid')
        auth_token = creds.get('auth_token') or token
        if not sid or not auth_token:
            raise ValueError('Twilio requires account_sid and auth_token')
        basic = base64.b64encode(f"{sid}:{auth_token}".encode()).decode()
        return {'Authorization': 'Basic ' + basic, 'Accept': 'application/json'}
    if p.key == 'stripe':
        return {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    if p.key == 'square':
        return {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    if p.key == 'yelp':
        return {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    return {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}


async def test_connection(p: Provider, token: str, extra: Optional[Dict[str, Any]] = None, credentials: Optional[Dict[str, Any]] = None):
    extra = extra or {}
    credentials = credentials or {}
    # Provider test paths are immutable. Account-specific base URLs are allowed only
    # when the provider explicitly has no fixed API base. This prevents the integration
    # test endpoint from becoming an arbitrary URL fetch primitive.
    base = p.api_base or extra.get('base_url') or credentials.get('base_url')
    path = p.test_path
    if path and '{tenant_id}' in path:
        tenant_id = credentials.get('tenant_id') or extra.get('tenant_id')
        if not tenant_id:
            return {'status': 'configured', 'message': 'Provider requires an account/tenant identifier before connectivity can be tested.', 'implementation': p.implementation}
        path = path.replace('{tenant_id}', quote(str(tenant_id), safe=''))
    if not base or not path:
        return {'status': 'configured', 'message': 'Credentials stored; provider requires account-specific endpoint/test configuration.', 'implementation': p.implementation}
    url = base.rstrip('/') + '/' + path.lstrip('/')
    validate_remote_url(url)
    headers = _auth_headers(p, credentials, token)
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False) as c:
        r = await c.get(url, headers=headers)
        if r.status_code >= 400:
            # Never surface the provider body to the API caller because provider errors can contain tokens or PII.
            raise RuntimeError(f'Provider returned HTTP {r.status_code}')
        return {'status': 'connected', 'http_status': r.status_code, 'implementation': p.implementation}


def verify_hmac(secret:str, body:bytes, signature:str, algorithm='sha256'):
    digest=hmac.new(secret.encode(),body,getattr(hashlib,algorithm)).hexdigest()
    return hmac.compare_digest(digest,signature)
