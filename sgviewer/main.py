import functools
import json
import os
import datetime
import itertools
import copy
import logging

from flask import Flask, request, render_template, redirect, url_for, abort, session

from sgsession import Session
import shotgun_api3_registry


log = logging.getLogger(__name__)


app = Flask(__name__,
    static_url_path='',
)
app.root_path = os.path.dirname(os.path.dirname(__file__))
app.config.from_object('sgviewer.config')


def Shotgun():
    return Session(shotgun_api3_registry.connect('sgviewer'))


def static(path):
    path = path.strip('/')
    real_path = os.path.join(app.root_path, 'static', path)
    if os.path.exists(real_path):
        return '/%s?mt=%d' % (path, os.path.getmtime(real_path))
    else:
        return '/%s' % path


@app.context_processor
def inject_into_templates():
    return dict(static=static)


@app.route('/', methods=['GET', 'POST'])
def index():
    return render_template('hello.html')


# Incoming from the Shotgun ActionMenuitem.
@app.route('/action_menu_item', methods=['POST'])
def action_menu_item():

    for key in ('user_id', 'user_login', 'session_uuid'):
        session[key] = request.form[key]

    entity_type = request.form['entity_type'].lower()
    entity_id = request.form['selected_ids'].split(',')[0]
    return redirect(url_for('view_one', entity_type=entity_type, entity_id=entity_id))


def minimal(entity):
    minimal = {'type': entity['type'], 'id': entity['id']}
    for k in ('code', 'name'):
        if k in entity:
            minimal[k] = entity[k]
    return minimal


def normalize_entity_type(entity_type):
    entity_type = entity_type.title()
    return {
        'Publish': 'PublishEvent',
        'Publishevent': 'PublishEvent',
    }.get(entity_type, entity_type)


@app.route('/latest_version/<entity_type>/<int:entity_id>')
def view_one(entity_type, entity_id):

    entity_type = normalize_entity_type(entity_type)
    sg = Shotgun()

    entity = sg.find_one(entity_type, [('id', 'is', entity_id)], [

        # Version fields:
        'code', 'sg_qt', 'description',

        # Shot/Task fields:
        'sg_latest_version.Version.sg_qt',

        # Publish fields:
        'sg_link.Task.sg_latest_version',
    ])

    if not entity:
        log.warning('Could not find %s %d', entity_type, entity_id)
        abort(404)


    latest_version = (
        entity.get('sg_latest_version') or 
        entity.get('sg_link.Task.sg_latest_version') or
        entity
    )
    if latest_version['type'] != 'Version':
        log.warning('Could not resolve version from %s %d; got %s %d',
            entity_type, entity_id,
            latest_version['type'] if latest_version else None,
            latest_version['id'] if latest_version else 0,
        )
        abort(404)

    # Make sure we have everything, since we wouldn't from a Publish.
    latest_version.fetch(['code', 'name', 'sg_qt', 'description'])

    # Fetch the breadcrumbs, working backwards from the entity that we were
    # given to make it obvious that it is coming from that Task, or Publish,
    # or whatever.
    breadcrumbs = [latest_version]
    if latest_version is not entity:
        breadcrumbs.insert(0, entity)
    while breadcrumbs[0]['type'] != 'Project':
        parent = breadcrumbs[0].parent()
        if parent is None:
            break
        breadcrumbs.insert(0, parent)

    # Get codes or names on all the breadcrumbs
    need_fetching = [x for x in breadcrumbs if all(k not in x for k in ('code', 'name'))]
    if need_fetching:
        sg.fetch(need_fetching, ['code', 'name'])

    video_url_dict = (
        entity.get('sg_qt') or
        entity.get('sg_latest_version.Version.sg_qt')
    ) or {}
    video_url = video_url_dict.get('url')
    if not video_url:
        abort(404)

    return render_template('view_one.html',
        entity=entity,
        latest_version=latest_version,
        breadcrumbs=[minimal(x) for x in breadcrumbs],
        video_url=video_url,
    )


_json_dt_handler = lambda obj: obj.isoformat() if isinstance(obj, datetime.datetime) else None

def api_endpoint(func):
    @functools.wraps(func)
    def _decorated(*args, **kwargs):
        return json.dumps(func(*args, **kwargs), indent=4, sort_keys=True, default=_json_dt_handler)
    return _decorated


@app.route('/shotgun/<request_type>', methods=['POST'])
@api_endpoint
def shotgun_api(request_type):
    sg = shotgun_api3_registry.connect()
    func = getattr(sg, request_type)
    print request.content_type
    return func(**request.json)


def _prepare_notes(notes):

    notes = list(set(notes))

    if not notes:
        return []

    sg = notes[0].session

    fields = ('id', 'type', 'created_by', 'created_at', 'subject', 'content', 'note_links')
    extra = ('created_by.HumanUser.image', )
    sg.fetch(notes, fields + extra)

    results = []
    for note in notes:
        note = dict((k, note[k]) for k in fields)
        links = note.pop('note_links')
        note['links'] = [{
            'type': link['type'],
            'id': link['id'],
            'code': link.get('code') or link.get('name'),
        } for link in links]
        results.append(note)

    results.sort(key=lambda e: e['created_at'])
    
    return results


@app.route('/history/<entity_type>/<int:entity_id>.json')
@api_endpoint
def history(entity_type, entity_id):

    entity_type = entity_type.title()
    sg = Shotgun()

    fields = (

        # Direct.
        'notes',

        # From a Version.
        'sg_task.Task.notes',
        'entity.Task.notes',
        'entity.Shot.notes',
        'entity.Asset.notes',

        # From a Task.
        'sg_latest_version.Version.notes',
        'entity.Shot.notes',

        # From a Shot.
        'sg_latest_version.Version.notes',

    )

    entity = sg.find_one(entity_type, [('id', 'is', entity_id)], fields)
    if not entity:
        abort(404)

    notes = itertools.chain(*[entity.get(field, []) for field in fields])

    parents = [entity]

    if entity['type'] == 'Version':
        entity = entity.fetch('entity')
        parents.append(entity)

    if entity['type'] == 'Task':
        entity = entity.fetch('entity')
        parents.append(entity)

    if entity['type'] not in ('Shot', 'Asset'):
        raise ValueError('Unusual version chain')

    versions = sg.find('Version',
        [('entity', 'is', e) for e in parents],
        ['code', 'description', 'user.HumanUser.name', 'user.HumanUser.image', 'created_at', 'sg_task.Task.content'],
        filter_operator='any',
    )
    
    notes = _prepare_notes(notes)

    for v in versions:
        v.pop('notes', None)
        v.get('entity', {}).pop('notes', None)
    versions = [x.as_dict() for x in versions]

    all_events = list(notes) + list(versions)
    all_events.sort(key=lambda e: e['created_at'])

    return {'events': all_events}


@app.route('/notes/new', methods=['POST'])
@api_endpoint
def new_note_api():

    sg = Shotgun()

    version = sg.merge({'type': 'Version', 'id': int(request.form['version_id'])})
    project, entity, publish, task = version.fetch(('project', 'entity', 'sg_publish', 'sg_task'))

    note = sg.create('Note', {
        'subject': 'Note on %s' % (version.get('code') or version.get('name')),
        'content': request.form['content'],
        'created_by': {'type': 'HumanUser', 'id': int(session['user_id'])},
        'user': {'type': 'HumanUser', 'id': int(session['user_id'])},
        'note_links': filter(None, [version, entity]),
        'project': project,
    })

    return _prepare_notes([note]);






