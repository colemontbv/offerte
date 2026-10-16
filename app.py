import os
import base64
import requests
from flask import Flask, render_template, request, jsonify, session, redirect, url_for

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'offerte-geheim-2024')

_raw_url = os.environ.get('ODOO_URL', '').strip().rstrip('/')
if _raw_url and not _raw_url.startswith(('http://', 'https://')):
    _raw_url = 'https://' + _raw_url
ODOO_URL = _raw_url
ODOO_DB  = os.environ.get('ODOO_DB', '')

COMPANY_NAME    = os.environ.get('COMPANY_NAME', 'Colemont')
COMPANY_WEBSITE = os.environ.get('COMPANY_WEBSITE', 'www.colemont.be')
COMPANY_EMAIL   = os.environ.get('COMPANY_EMAIL', '')
COMPANY_PHONE   = os.environ.get('COMPANY_PHONE', '')


def odoo_rpc(cookies, model, method, args, kwargs=None):
    return requests.post(
        f'{ODOO_URL}/web/dataset/call_kw',
        json={'jsonrpc': '2.0', 'method': 'call', 'id': 1,
              'params': {'model': model, 'method': method,
                         'args': args, 'kwargs': kwargs or {}}},
        cookies=cookies,
        timeout=30,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if session.get('odoo_session'):
        return redirect(url_for('catalog'))
    return render_template('login.html', company_name=COMPANY_NAME)


@app.route('/login', methods=['POST'])
def login():
    data = request.json or {}
    try:
        resp = requests.post(
            f'{ODOO_URL}/web/session/authenticate',
            json={'jsonrpc': '2.0', 'method': 'call', 'id': 1,
                  'params': {'db': ODOO_DB,
                             'login': data.get('login', ''),
                             'password': data.get('password', '')}},
            timeout=15,
        )
        r = resp.json().get('result', {})
        if not (r and r.get('uid')):
            return jsonify({'success': False, 'error': 'Ongeldige inloggegevens'})

        odoo_cookie = resp.cookies.get('session_id')

        # Enkel portaalgebruikers (share=True) mogen inloggen
        try:
            share_resp = requests.post(
                f'{ODOO_URL}/web/dataset/call_kw',
                json={'jsonrpc': '2.0', 'method': 'call', 'id': 1,
                      'params': {'model': 'res.users', 'method': 'read',
                                 'args': [[r['uid']], ['share', 'partner_id']],
                                 'kwargs': {}}},
                cookies={'session_id': odoo_cookie}, timeout=15,
            )
            user_data = share_resp.json().get('result', [{}])[0]
            is_portal = bool(user_data.get('share'))
        except Exception:
            return jsonify({'success': False, 'error': 'Kon gebruiker niet verifiëren'})

        if not is_portal:
            return jsonify({'success': False,
                            'error': 'Deze applicatie is enkel voor klanten. Interne medewerkers kunnen niet inloggen.'})

        partner = user_data.get('partner_id', [])
        session['odoo_session'] = odoo_cookie
        session['uid']          = r['uid']
        session['user_name']    = r.get('name', '')
        session['user_login']   = data.get('login', '').strip().lower()
        session['partner_id']   = partner[0] if isinstance(partner, list) else None
        session['partner_name'] = partner[1] if isinstance(partner, list) and len(partner) > 1 else r.get('name', '')
        return jsonify({'success': True})

    except requests.exceptions.ConnectionError:
        return jsonify({'success': False, 'error': f'Kan geen verbinding maken met de server'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/catalog')
def catalog():
    if not session.get('odoo_session'):
        return redirect(url_for('index'))
    return render_template('catalog.html',
                           company_name=COMPANY_NAME,
                           partner_name=session.get('partner_name', ''))


@app.route('/api/products')
def api_products():
    if not session.get('odoo_session'):
        return jsonify({'error': 'Niet ingelogd'}), 401
    cookies = {'session_id': session['odoo_session']}
    try:
        # Zoek Default pricelist op voor correcte prijzen
        pl_resp = odoo_rpc(cookies, 'product.pricelist', 'search_read',
                           [[['name', 'ilike', 'Default']]],
                           {'fields': ['id', 'name'], 'limit': 1})
        pricelists = pl_resp.json().get('result', [])
        pricelist_id = pricelists[0]['id'] if pricelists else None
        ctx = {'pricelist': pricelist_id} if pricelist_id else {}

        # Haal webcategorieën op
        cat_resp = odoo_rpc(cookies, 'product.public.category', 'search_read',
                            [[]],
                            {'fields': ['id', 'name', 'complete_name'], 'limit': 500})
        cats = {c['id']: c for c in cat_resp.json().get('result', [])}

        # Haal producten op met pricelist context
        resp = odoo_rpc(cookies, 'product.template', 'search_read',
                        [[['is_published', '=', True],
                          ['sale_ok', '=', True]]],
                        {'fields': ['id', 'name', 'description_sale', 'lst_price',
                                    'public_categ_ids', 'uom_id', 'default_code'],
                         'context': ctx,
                         'order': 'name asc',
                         'limit': 1000})
        products = resp.json().get('result', [])

        # Koppel categorienamen aan elk product
        for p in products:
            p['web_categories'] = [
                {'id': cid, 'name': cats[cid]['name'],
                 'complete_name': cats[cid].get('complete_name', cats[cid]['name'])}
                for cid in p.get('public_categ_ids', []) if cid in cats
            ]

        return jsonify({'products': products,
                        'categories': sorted(cats.values(), key=lambda c: c.get('complete_name',''))})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/product/<int:tmpl_id>/image')
def product_image(tmpl_id):
    if not session.get('odoo_session'):
        return '', 401
    cookies = {'session_id': session['odoo_session']}
    try:
        resp = odoo_rpc(cookies, 'product.template', 'read',
                        [[tmpl_id], ['image_512']], {})
        data = resp.json().get('result', [{}])[0]
        img_b64 = data.get('image_512', False)
        if not img_b64:
            return '', 204
        img_bytes = base64.b64decode(img_b64)
        from flask import Response
        return Response(img_bytes, mimetype='image/jpeg',
                        headers={'Cache-Control': 'public, max-age=3600'})
    except Exception:
        return '', 500


@app.route('/api/partner')
def api_partner():
    if not session.get('odoo_session'):
        return jsonify({'error': 'Niet ingelogd'}), 401
    cookies  = {'session_id': session['odoo_session']}
    pid      = session.get('partner_id')
    if not pid:
        return jsonify({'partner': {}})
    try:
        resp = odoo_rpc(cookies, 'res.partner', 'read',
                        [[pid], ['name', 'street', 'zip', 'city',
                                 'country_id', 'vat', 'email', 'phone']], {})
        data = resp.json().get('result', [{}])[0]
        return jsonify({'partner': data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/quote')
def quote_preview():
    if not session.get('odoo_session'):
        return redirect(url_for('index'))
    return render_template('quote.html',
                           company_name=COMPANY_NAME,
                           company_website=COMPANY_WEBSITE,
                           company_email=COMPANY_EMAIL,
                           company_phone=COMPANY_PHONE,
                           partner_name=session.get('partner_name', ''))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5020))
    app.run(debug=True, port=port)
