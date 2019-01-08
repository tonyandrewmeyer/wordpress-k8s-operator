import io
from pprint import pprint
import re
import string
import yaml

from charms.layer import caas_base, status
from charms import reactive
from charms.reactive import hook, when, when_any, when_none, tracer
from charmhelpers.core import hookenv


# Run the create_container handler again whenever the config or
# database relation changes.
reactive.register_trigger(when='config.changed', clear_flag='container.configured')
reactive.register_trigger(when='postgres.master.changed', clear_flag='container.configured')

reactive.register_trigger(when='config.changed.container_config', clear_flag='container.no-postgres')
reactive.register_trigger(when='config.changed.container_secrets', clear_flag='container.no-postgres')


@hook('upgrade-charm')
def upgrade_charm():
    reactive.clear_flag('container.no-postgres')
    reactive.clear_flag('container.configured')


@when('config.default.image')
def block_on_image_config():
    status.blocked("config item 'image' is required")


@when_none('postgres.connected', 'container.no-postgres')
def block_for_postgres():
    if postgres_required():
        hookenv.log("PostgreSQL relation is required")
        status.blocked('postgres relation is required')
    else:
        hookenv.log("PostgreSQL relation is not required")
        reactive.set_flag('container.no-postgres')


@when('postgres.connected')
@when_none('postgres.master.available', 'container.no-postgres')
def wait_for_postgres():
    status.waiting('Waiting for postgres relation to complete')


reactive.register_trigger(when='config.changed.pgdatabase', clear_flag='container.postgres.dbset')
reactive.register_trigger(when='config.changed.pgextensions', clear_flag='container.postgres.dbset')
reactive.register_trigger(when='config.changed.pgroles', clear_flag='container.postgres.dbset')


@when('postgresql.connected')
@when_none('container.postgres.dbset')
def setup_postgres():
    pgsql = reactive.endpoint_from_name('postgres')
    if pgsql is None:
        hookenv.log("Expected postgres relation was not found", hookenv.ERROR)
    config = hookenv.config()
    pgsql.set_database(config['pgdatabase'])
    pgsql.set_extensions(set(config['pgextensions'].split(',')))
    pgsql.set_roles(set(config['pgroles'].split(',')))
    reactive.set_flag('container.postgres.dbset')


@when_none('container.configured', 'config.default.image')
@when_any('postgres.master.available', 'container.no-postgres')
def config_container():
    status.maintenance('configuring container')
    spec = make_pod_spec()
    if caas_base.pod_spec_set(spec):
        reactive.set_flag('container.configured')
        status.active('pods active')
    else:
        status.blocked('k8s spec deployment failed. Check logs with kubectl')


def postgres_required():
    '''True if container config contains ${PGHOST} style vars'''
    c = full_container_config()
    p = re.compile(r'\$\{?PG\w+\}?', re.I)
    for k, v in c:
        if p.search(v) is not None:
            return True
    return False


def sanitized_container_config():
    '''Uninterpolated container config without secrets'''
    config = hookenv.config()
    return yaml.safe_load(config['container_config'])


def full_container_config():
    '''Uninterpolated ontainer config with secrets'''
    config = hookenv.config()
    container_config = yaml.safe_load(config['container_config'])
    container_config.update(yaml.safe_load(config['container_secrets']))
    return container_config


def interpolate(container_config):
    '''Use string.Template to interpolate supported placeholders'''
    context = {}
    pgsql = reactive.endpoint_from_name('postgres')
    if pgsql is not None and pgsql.master is not None:
        master = pgsql.master
        context['PGHOST'] = master.host
        context['PGDATABASE'] = master.dbname
        context['PGPORT'] = master.port
        context['PGUSER'] = master.user
        context['PGPASSWORD'] = master.password
        context['PGURI'] = master.uri
    iconfig = {}
    for k, v in container_config:
        t = string.Template(v)
        iconfig[k] = t.safe_substitute(context)
    return iconfig


def make_pod_spec():
    config = hookenv.config()
    container_config = interpolate(sanitized_container_config())

    ports = [
        {'name': name, 'containerPort': int(port), 'protocol': 'TCP'} for name, port in [
            addr.split(':', 1) for addr in config['ports'].split()]]

    # PodSpec v1? https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.13/#podspec-v1-core
    spec = {
        'containers': [
            {
                'name': hookenv.charm_name(),
                'image': config['image'],
                'ports': ports,
                'config': container_config,
            },
        ],
    }
    out = io.StringIO()
    pprint(spec, out)
    hookenv.log("Container spec (sans secrets) <<EOM\n{}\nEOM".format(out.getvalue()))

    # Add the secrets after logging
    container_config.update(interpolate(full_container_config()))

    return spec


def resource_image_details(spec):
    """Add image retrieval stanza for images attached as a Juju oci-image resource.

    This is not being used, and just here for reference.
    """
    # Grab the details from resource-get.
    image_details_path = hookenv.resource_get("image")
    if not image_details_path:
        raise Exception("unable to retrieve image details")

    with open(image_details_path, "r") as f:
        image_details = yaml.safe_load(f)

    docker_image_path = image_details['registrypath']
    docker_image_username = image_details['username']
    docker_image_password = image_details['password']
    spec['imageDetails'] = {
        'imagePath': docker_image_path,
        'username': docker_image_username,
        'password': docker_image_password,
    }


# Magic. Add useful flag logging. Remove after next charms.reactive release.
hookenv.atstart(tracer.install_tracer, tracer.LogTracer())
