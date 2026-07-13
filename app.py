import os
import sqlite3
import json
import csv
import io
from flask import Flask, send_from_directory, jsonify, request, g, Response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
DATABASE = os.environ.get('DATABASE_PATH', os.path.join(BASE_DIR, 'leads.db'))


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    with open(os.path.join(BASE_DIR, 'schema.sql'), 'r') as f:
        db.executescript(f.read())
    db.commit()
    db.close()


def seed_db():
    db = sqlite3.connect(DATABASE)
    count = db.execute('SELECT COUNT(*) FROM leads').fetchone()[0]
    if count == 0:
        json_path = os.path.join(BASE_DIR, 'leads_import.json')
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8-sig') as f:
                leads = json.load(f)
            auto_reasons = {
                1: 'Already running paid ads — budget-aware, expects ROI',
                2: 'Strong organic presence — ready for paid amplification',
                3: 'Low digital presence — needs full buildout'
            }
            for lead in leads:
                priority = int(lead.get('priority', 3))
                priority_reason = lead.get('priority_reason') or auto_reasons.get(priority, '')
                try:
                    db.execute('''
                        INSERT OR IGNORE INTO leads (business_name, business_type, phone, website, instagram,
                                       address, city, rating, review_count, priority, priority_reason, ad_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', [
                        lead.get('business_name'), lead.get('business_type'), lead.get('phone'),
                        lead.get('website'), lead.get('instagram'), lead.get('address'),
                        lead.get('city'), lead.get('rating'), lead.get('review_count'),
                        priority, priority_reason, lead.get('ad_status', 'unknown')
                    ])
                except Exception as e:
                    print(f'Seed error: {e}')
            db.commit()
            print(f'[seed] Auto-loaded {len(leads)} leads from leads_import.json')
    db.close()


with app.app_context():
    init_db()
    seed_db()


@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/api/leads')
def get_leads():
    db = get_db()
    priority = request.args.get('priority')
    contacted = request.args.get('contacted')
    business_type = request.args.get('type')
    search = request.args.get('search', '')

    query = 'SELECT * FROM leads WHERE 1=1'
    params = []

    if priority:
        query += ' AND priority = ?'
        params.append(priority)
    if contacted is not None and contacted != '':
        query += ' AND contacted = ?'
        params.append(1 if contacted == 'true' else 0)
    if business_type:
        query += ' AND business_type = ?'
        params.append(business_type)
    if search:
        query += ' AND (business_name LIKE ? OR city LIKE ? OR business_type LIKE ?)'
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])

    query += ' ORDER BY priority ASC, COALESCE(rating, 0) DESC'

    leads = db.execute(query, params).fetchall()
    return jsonify([dict(lead) for lead in leads])


@app.route('/api/leads/stats')
def get_stats():
    db = get_db()
    total = db.execute('SELECT COUNT(*) FROM leads').fetchone()[0]
    p1 = db.execute('SELECT COUNT(*) FROM leads WHERE priority = 1').fetchone()[0]
    p2 = db.execute('SELECT COUNT(*) FROM leads WHERE priority = 2').fetchone()[0]
    p3 = db.execute('SELECT COUNT(*) FROM leads WHERE priority = 3').fetchone()[0]
    contacted = db.execute('SELECT COUNT(*) FROM leads WHERE contacted = 1').fetchone()[0]
    return jsonify({'total': total, 'p1': p1, 'p2': p2, 'p3': p3, 'contacted': contacted})


@app.route('/api/leads/<int:lead_id>', methods=['PUT'])
def update_lead(lead_id):
    db = get_db()
    data = request.json

    updates = []
    params = []

    allowed = ['contacted', 'notes', 'dm_generated', 'priority', 'instagram', 'phone', 'website', 'ad_status']
    for field in allowed:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])

    if data.get('contacted') is True:
        updates.append('contacted_date = datetime("now")')
    elif data.get('contacted') is False:
        updates.append('contacted_date = NULL')

    if not updates:
        return jsonify({'success': True})

    params.append(lead_id)
    db.execute(f'UPDATE leads SET {", ".join(updates)} WHERE id = ?', params)
    db.commit()
    return jsonify({'success': True})


@app.route('/api/leads/<int:lead_id>/dm', methods=['POST'])
def generate_dm(lead_id):
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY not set'}), 500

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        return jsonify({'error': 'anthropic package not installed'}), 500

    db = get_db()
    lead = db.execute('SELECT * FROM leads WHERE id = ?', [lead_id]).fetchone()
    if not lead:
        return jsonify({'error': 'Lead not found'}), 404

    lead = dict(lead)

    ad_context = {
        'running_ads': 'they are currently running paid ads (Meta, Google, or other platforms)',
        'organic_only': 'they have strong organic social presence but no visible paid ads',
        'no_presence': 'they have little to no online presence',
        'unknown': 'their ad status is unknown'
    }.get(lead['ad_status'], 'unknown')

    priority_context = {
        1: 'high priority — already investing in paid advertising, understands ad spend',
        2: 'solid organic presence — natural next step toward paid ads',
        3: 'low digital presence — needs full buildout'
    }.get(lead['priority'], '')

    prompt = f"""Write a personalized cold Instagram DM for this business.

Business: {lead['business_name']}
Type: {lead['business_type'] or 'health & wellness'}
City: {lead['city'] or 'California'}
Situation: {ad_context}
Context: {priority_context}

Rules — follow every single one:
- Max 3-4 lines total
- Casual, fun opener that sounds like a real person stumbled on their page, not a marketer
- Reference something specific to their niche or situation
- Zero mention of services, agencies, ads, or marketing
- End with exactly ONE genuine question about their business
- Goal is to get a reply, not sell anything
- Never use em-dashes (—), lists of three things in a row, the word "imagine", phrases like "it goes without saying", Q&A format, or multiple exclamation marks
- No robotic grammar or formal sentence structure
- Write like you're texting someone casually, not writing copy

Return only the DM text. No quotes, no labels, nothing else."""

    message = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=200,
        messages=[{'role': 'user', 'content': prompt}]
    )

    dm_text = message.content[0].text.strip()

    db.execute('UPDATE leads SET dm_generated = ? WHERE id = ?', [dm_text, lead_id])
    db.commit()

    return jsonify({'dm': dm_text})


@app.route('/api/leads', methods=['POST'])
def add_lead():
    db = get_db()
    data = request.json

    priority = int(data.get('priority', 3))
    auto_reasons = {
        1: 'Already running paid ads — budget-aware, expects ROI',
        2: 'Strong organic presence — ready for paid amplification',
        3: 'Low digital presence — needs full buildout'
    }
    priority_reason = data.get('priority_reason') or auto_reasons.get(priority, '')

    try:
        db.execute('''
            INSERT INTO leads (business_name, business_type, phone, website, instagram,
                               address, city, rating, review_count, priority, priority_reason, ad_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', [
            data.get('business_name'), data.get('business_type'), data.get('phone'),
            data.get('website'), data.get('instagram'), data.get('address'),
            data.get('city'), data.get('rating'), data.get('review_count'),
            priority, priority_reason, data.get('ad_status', 'unknown')
        ])
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Lead already exists'}), 409

    return jsonify({'success': True})


@app.route('/api/import', methods=['POST'])
def import_leads():
    db = get_db()
    data = request.json
    leads = data.get('leads', [])

    auto_reasons = {
        1: 'Already running paid ads — budget-aware, expects ROI',
        2: 'Strong organic presence — ready for paid amplification',
        3: 'Low digital presence — needs full buildout'
    }

    imported = 0
    skipped = 0
    for lead in leads:
        priority = int(lead.get('priority', 3))
        priority_reason = lead.get('priority_reason') or auto_reasons.get(priority, '')
        try:
            db.execute('''
                INSERT OR IGNORE INTO leads (business_name, business_type, phone, website, instagram,
                               address, city, rating, review_count, priority, priority_reason, ad_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', [
                lead.get('business_name'), lead.get('business_type'), lead.get('phone'),
                lead.get('website'), lead.get('instagram'), lead.get('address'),
                lead.get('city'), lead.get('rating'), lead.get('review_count'),
                priority, priority_reason, lead.get('ad_status', 'unknown')
            ])
            if db.execute('SELECT changes()').fetchone()[0]:
                imported += 1
            else:
                skipped += 1
        except Exception as e:
            print(f'Import error: {e}')
            skipped += 1

    db.commit()
    return jsonify({'success': True, 'imported': imported, 'skipped': skipped})


@app.route('/api/export')
def export_leads():
    db = get_db()
    leads = db.execute('SELECT * FROM leads ORDER BY priority, rating DESC').fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Business', 'Type', 'City', 'Phone', 'Website', 'Instagram',
                     'Rating', 'Reviews', 'Priority', 'Priority Reason', 'Ad Status',
                     'Contacted', 'Notes', 'DM', 'Created'])
    for lead in leads:
        d = dict(lead)
        writer.writerow([
            d['id'], d['business_name'], d['business_type'], d['city'], d['phone'],
            d['website'], d['instagram'], d['rating'], d['review_count'],
            d['priority'], d['priority_reason'], d['ad_status'],
            'Yes' if d['contacted'] else 'No', d['notes'], d['dm_generated'], d['created_at']
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=vantg-leads.csv'}
    )


@app.route('/api/leads/<int:lead_id>', methods=['DELETE'])
def delete_lead(lead_id):
    db = get_db()
    db.execute('DELETE FROM leads WHERE id = ?', [lead_id])
    db.commit()
    return jsonify({'success': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
