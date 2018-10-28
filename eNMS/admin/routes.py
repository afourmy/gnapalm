from flask import (
    abort,
    current_app as app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for
)
from flask_login import current_user, login_user, logout_user
from pynetbox import api as netbox_api
from sqlalchemy.orm.exc import NoResultFound
from tacacs_plus.client import TACACSClient
from tacacs_plus.flags import TAC_PLUS_AUTHEN_TYPE_ASCII
from requests import get as http_get
from yaml import dump, load

from eNMS import db
from eNMS.admin import bp
from eNMS.admin.forms import (
    AddUser,
    CreateAccountForm,
    LoginForm,
    GeographicalParametersForm,
    GottyParametersForm,
    SyslogServerForm,
    TacacsServerForm,
)
from eNMS.admin.models import (
    Parameters,
    User,
    TacacsServer
)
from eNMS.automation.models import service_classes
from eNMS.base.classes import classes, diagram_classes
from eNMS.base.custom_base import factory
from eNMS.base.helpers import (
    get,
    objectify,
    post,
    fetch,
    vault_helper
)
from eNMS.base.properties import (
    import_properties,
    pretty_names,
    serialization_properties,
    user_public_properties
)
from eNMS.logs.models import SyslogServer
from eNMS.objects.models import Device


@get(bp, '/user_management', 'Admin Section')
def users():
    form = AddUser(request.form)
    return render_template(
        'user_management.html',
        fields=user_public_properties,
        names=pretty_names,
        users=User.serialize(),
        form=form
    )


@get(bp, '/migration', 'Admin Section')
def migration():
    return render_template('migration.html')


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        name = str(request.form['name'])
        user_password = str(request.form['password'])
        user = fetch(User, name=name)
        if user:
            if app.config['USE_VAULT']:
                pwd = vault_helper(app, f'user/{user.name}')['password']
            else:
                pwd = user.password
            if user_password == pwd:
                login_user(user)
                return redirect(url_for('base_blueprint.dashboard'))
        else:
            try:
                # tacacs_plus does not support py2 unicode, hence the
                # conversion to string.
                # TACACSClient cannot be saved directly to session
                # as it is not serializable: this temporary fixes will create
                # a new instance of TACACSClient at each TACACS connection
                # attemp: clearly suboptimal, to be improved later.
                tacacs_server = db.session.query(TacacsServer).one()
                tacacs_client = TACACSClient(
                    str(tacacs_server.ip_address),
                    int(tacacs_server.port),
                    str(tacacs_server.password)
                )
                if tacacs_client.authenticate(
                    name,
                    user_password,
                    TAC_PLUS_AUTHEN_TYPE_ASCII
                ).valid:
                    user = User(name=name, password=user_password)
                    db.session.add(user)
                    db.session.commit()
                    login_user(user)
                    return redirect(url_for('base_blueprint.dashboard'))
            except NoResultFound:
                pass
        return render_template('errors/page_403.html')
    if not current_user.is_authenticated:
        return render_template(
            'login.html',
            login_form=LoginForm(request.form),
            create_account_form=CreateAccountForm(request.form)
        )
    return redirect(url_for('base_blueprint.dashboard'))


@get(bp, '/logout')
def logout():
    logout_user()
    return redirect(url_for('admin_blueprint.login'))


@get(bp, '/administration', 'Admin Section')
def admninistration():
    try:
        tacacs_server = db.session.query(TacacsServer).one()
    except NoResultFound:
        tacacs_server = None
    try:
        syslog_server = db.session.query(SyslogServer).one()
    except NoResultFound:
        syslog_server = None
    return render_template(
        'administration.html',
        geographical_parameters_form=GeographicalParametersForm(request.form),
        gotty_parameters_form=GottyParametersForm(request.form),
        parameters=db.session.query(Parameters).one(),
        tacacs_form=TacacsServerForm(request.form),
        syslog_form=SyslogServerForm(request.form),
        tacacs_server=tacacs_server,
        syslog_server=syslog_server
    )


@post(bp, '/create_new_user', 'Edit Admin Section')
def create_new_user():
    user_data = request.form.to_dict()
    if 'permissions' in user_data:
        abort(403)
    return jsonify(factory(User, **user_data).serialized)


@post(bp, '/process_user', 'Edit Admin Section')
def process_user():
    user_data = request.form.to_dict()
    user_data['permissions'] = request.form.getlist('permissions')
    return jsonify(factory(User, **user_data).serialized)


@post(bp, '/get/<user_id>', 'Admin Section')
def get_user(user_id):
    user = fetch(User, id=user_id)
    return jsonify(user.serialized)


@post(bp, '/delete/<user_id>', 'Edit Admin Section')
def delete_user(user_id):
    user = fetch(User, id=user_id)
    db.session.delete(user)
    db.session.commit()
    return jsonify(True)


@post(bp, '/save_tacacs_server', 'Edit parameters')
def save_tacacs_server():
    TacacsServer.query.delete()
    tacacs_server = TacacsServer(**request.form.to_dict())
    db.session.add(tacacs_server)
    db.session.commit()
    return jsonify(True)


@post(bp, '/save_syslog_server', 'Edit parameters')
def save_syslog_server():
    SyslogServer.query.delete()
    syslog_server = SyslogServer(**request.form.to_dict())
    db.session.add(syslog_server)
    db.session.commit()
    return jsonify(True)


@post(bp, '/save_geographical_parameters', 'Edit parameters')
def save_geographical_parameters():
    db.session.query(Parameters).one().update(**request.form.to_dict())
    db.session.commit()
    return jsonify(True)


@post(bp, '/save_gotty_parameters', 'Edit parameters')
def save_gotty_parameters():
    db.session.query(Parameters).one().update(**request.form.to_dict())
    db.session.commit()
    return jsonify(True)
