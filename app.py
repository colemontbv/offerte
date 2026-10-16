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
    debug = []

    # Zoek Default pricelist (kan falen voor portaalgebruikers — geen blocker)
    pricelist_id = None
    try:
        pl_resp = odoo_rpc(cookies, 'product.pricelist', 'search_read',
                           [[['name', 'ilike', 'Default']]],
                           {'fields': ['id', 'name'], 'limit': 1})
        pl_data = pl_resp.json()
        pricelists = pl_data.get('result', [])
        if pricelists:
            pricelist_id = pricelists[0]['id']
            debug.append(f'pricelist: {pricelist_id} ({pricelists[0]["name"]})')
        else:
            debug.append(f'pricelist: not found — {pl_data.get("error", {}).get("message","?")}')
    except Exception as e:
        debug.append(f'pricelist error: {e}')

    ctx = {'pricelist': pricelist_id, 'lang': 'nl_BE'} if pricelist_id else {'lang': 'nl_BE'}

    # Haal producten op (inclusief variant-telling)
    try:
        kwargs = {'fields': ['id', 'name', 'description_sale', 'list_price',
                              'public_categ_ids', 'uom_id', 'default_code',
                              'product_variant_count'],
                  'order': 'name asc',
                  'limit': 1000,
                  'context': ctx}
        resp = odoo_rpc(cookies, 'product.template', 'search_read',
                        [[['is_published', '=', True], ['sale_ok', '=', True]]],
                        kwargs)
        resp_data = resp.json()
        products = resp_data.get('result', [])
        if not products and resp_data.get('error'):
            debug.append(f'products error: {resp_data["error"].get("message","?")}')
            return jsonify({'error': resp_data['error'].get('message','Odoo fout'),
                            'debug': debug}), 500
        debug.append(f'products: {len(products)}')
    except Exception as e:
        debug.append(f'products exception: {e}')
        return jsonify({'error': str(e), 'debug': debug}), 500

    # Bouw prijskaart uit pricelist items (fixed price per product template / variant)
    price_map = {}  # tmpl_id -> price  OR  ('v', var_id) -> price
    if pricelist_id:
        try:
            items_resp = odoo_rpc(cookies, 'product.pricelist.item', 'search_read',
                                  [[['pricelist_id', '=', pricelist_id],
                                    ['compute_price', '=', 'fixed']]],
                                  {'fields': ['applied_on', 'product_tmpl_id',
                                              'product_id', 'fixed_price'],
                                   'limit': 5000})
            for item in items_resp.json().get('result', []) or []:
                if item.get('applied_on') == '1_product' and item.get('product_tmpl_id'):
                    tmpl_id = item['product_tmpl_id'][0]
                    if tmpl_id not in price_map:
                        price_map[tmpl_id] = item['fixed_price']
                elif item.get('applied_on') == '0_product_variant' and item.get('product_id'):
                    var_id = item['product_id'][0]
                    price_map[('v', var_id)] = item['fixed_price']
            debug.append(f'pricelist items: {len(price_map)}')
        except Exception as e:
            debug.append(f'pricelist items error: {e}')

    # Bereken minimumprijs voor producten met varianten
    variant_min = {}  # tmpl_id -> min variant price
    variant_tmpl_ids = [p['id'] for p in products if (p.get('product_variant_count') or 0) > 1]
    if variant_tmpl_ids:
        try:
            vr = odoo_rpc(cookies, 'product.product', 'search_read',
                          [[['product_tmpl_id', 'in', variant_tmpl_ids], ['active', '=', True]]],
                          {'fields': ['id', 'product_tmpl_id', 'lst_price'],
                           'context': ctx, 'limit': 5000})
            for v in vr.json().get('result', []) or []:
                tid = v['product_tmpl_id'][0] if v.get('product_tmpl_id') else None
                if not tid:
                    continue
                vp = price_map.get(('v', v['id']), v.get('lst_price') or 0)
                if vp > 0 and (tid not in variant_min or vp < variant_min[tid]):
                    variant_min[tid] = vp
            debug.append(f'variant min prices: {len(variant_min)}')
        except Exception as e:
            debug.append(f'variant min error: {e}')

    # Voeg display_price en price_prefix toe aan elk product
    for p in products:
        vc = p.get('product_variant_count') or 0
        if vc > 1 and p['id'] in variant_min:
            p['display_price'] = variant_min[p['id']]
            p['price_prefix']  = 'Vanaf'
        else:
            p['display_price'] = price_map.get(p['id'], p['list_price'] or 0)
            p['price_prefix']  = ''

    # Haal categorienamen en ouder-kind-relaties op via read met IDs uit producten
    cats = {}
    try:
        all_cat_ids = list({cid for p in products for cid in p.get('public_categ_ids', [])})
        if all_cat_ids:
            cat_resp = odoo_rpc(cookies, 'product.public.category', 'read',
                                [all_cat_ids, ['id', 'name', 'parent_id']], {})
            cats = {c['id']: c for c in (cat_resp.json().get('result', []) or []) if c}
            # Voeg ook parent-records toe die niet zelf op producten staan
            missing_parent_ids = list({
                c['parent_id'][0]
                for c in cats.values()
                if c.get('parent_id') and c['parent_id'][0] not in cats
            })
            if missing_parent_ids:
                pr = odoo_rpc(cookies, 'product.public.category', 'read',
                              [missing_parent_ids, ['id', 'name', 'parent_id']], {})
                for c in (pr.json().get('result', []) or []):
                    if c:
                        cats[c['id']] = c
        debug.append(f'categories: {len(cats)} (from {len(all_cat_ids)} ids)')
    except Exception as e:
        debug.append(f'categories error: {e}')

    # Koppel categorienamen aan producten
    for p in products:
        p['web_categories'] = [
            {'id': cid, 'name': cats[cid]['name']}
            for cid in p.get('public_categ_ids', []) if cid in cats
        ]

    return jsonify({'products': products,
                    'categories': sorted(cats.values(), key=lambda c: c.get('name', '')),
                    'debug': debug})


@app.route('/api/product/<int:tmpl_id>/variants')
def product_variants(tmpl_id):
    if not session.get('odoo_session'):
        return jsonify({'error': 'Niet ingelogd'}), 401
    cookies = {'session_id': session['odoo_session']}
    try:
        # Haal pricelist op voor prijscontext
        pricelist_id = None
        pl_resp = odoo_rpc(cookies, 'product.pricelist', 'search_read',
                           [[['name', 'ilike', 'Default']]],
                           {'fields': ['id'], 'limit': 1})
        pl_data = pl_resp.json().get('result', [])
        if pl_data:
            pricelist_id = pl_data[0]['id']

        # Haal varianten op
        kwargs = {'fields': ['id', 'display_name', 'lst_price',
                             'product_template_attribute_value_ids', 'default_code'],
                  'limit': 100}
        if pricelist_id:
            kwargs['context'] = {'pricelist': pricelist_id}
        var_resp = odoo_rpc(cookies, 'product.product', 'search_read',
                            [[['product_tmpl_id', '=', tmpl_id], ['active', '=', True]]],
                            kwargs)
        variants = var_resp.json().get('result', []) or []

        # Haal pricelist variant-items op voor eventuele fixed prices
        price_map = {}
        if pricelist_id and variants:
            var_ids = [v['id'] for v in variants]
            items_resp = odoo_rpc(cookies, 'product.pricelist.item', 'search_read',
                                  [[['pricelist_id', '=', pricelist_id],
                                    ['compute_price', '=', 'fixed'],
                                    ['applied_on', '=', '0_product_variant'],
                                    ['product_id', 'in', var_ids]]],
                                  {'fields': ['product_id', 'fixed_price'], 'limit': 200})
            for item in items_resp.json().get('result', []) or []:
                if item.get('product_id'):
                    price_map[item['product_id'][0]] = item['fixed_price']

        for v in variants:
            v['display_price'] = price_map.get(v['id'], v.get('lst_price', 0))
            # Bouw een korte variantnaam (enkel het variante deel, zonder templatenaam)
            full = v.get('display_name', '')
            if '(' in full and full.endswith(')'):
                v['variant_label'] = full[full.index('(')+1:-1]
            else:
                v['variant_label'] = full

        return jsonify({'variants': variants})
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
