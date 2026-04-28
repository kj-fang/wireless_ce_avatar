from __future__ import annotations


class Securable:
    """Class to represent an snowflake entity that is being secured by a :class:`Privileges`."""

    def __init__(self, name: str, securable_type: str, scope: Securable | None = None) -> None:
        self._name = name
        self._securable_type = securable_type
        self._scope = scope

    @property
    def name(self) -> str:
        """String that specifies the name of resource that is being secured by a privilege."""
        return self._name

    @property
    def securable_type(self) -> str:
        """String that specifies the type of resource that is being secured by a privilege."""
        return self._securable_type

    @property
    def scope(self) -> Securable | None:
        """This is the scope of the resource that is being secured by a privilege."""
        return self._scope


class Securables:
    """
    Util Class with static method to create various ``Securable`` class instances.

    Examples
    ________
    Creating a Securable:

    >>> Securables.account("test-account")
    >>> Securables.database("testdb")
    >>> Securables.current_account
    """

    current_account = Securable("-", "account")

    @staticmethod
    def database(name: str) -> Securable:
        """Securable for a database."""
        return Securable(name, "database")

    @staticmethod
    def database_role(name: str) -> Securable:
        """Securable for a database role."""
        return Securable(name, "database role")

    @staticmethod
    def application_role(name: str) -> Securable:
        """Securable for a application role."""
        return Securable(name, "application role")

    @staticmethod
    def service_role(name: str) -> Securable:
        """Securable for a service role."""
        return Securable(name, "service role")

    @staticmethod
    def role(name: str) -> Securable:
        """Securable for a role."""
        return Securable(name, "role")

    @staticmethod
    def account(name: str) -> Securable:
        """Securable for an account."""
        return Securable(name, "account")

    @staticmethod
    def schema(name: str) -> Securable:
        """Securable for a schema."""
        return Securable(name, "schema")

    @staticmethod
    def integration(name: str) -> Securable:
        """Securable for an integration."""
        return Securable(name, "integration")

    @staticmethod
    def user(name: str) -> Securable:
        """Securable for a user."""
        return Securable(name, "user")

    @staticmethod
    def resource_monitor(name: str) -> Securable:
        """Securable for a resource monitor."""
        return Securable(name, "RESOURCE MONITOR")

    @staticmethod
    def warehouse(name: str) -> Securable:
        """Securable for a warehouse."""
        return Securable(name, "WAREHOUSE")

    @staticmethod
    def compute_pool(name: str) -> Securable:
        """Securable for a compute pool."""
        return Securable(name, "COMPUTE POOL")

    @staticmethod
    def connection(name: str) -> Securable:
        """Securable for a connection."""
        return Securable(name, "CONNECTION")

    @staticmethod
    def failover_group(name: str) -> Securable:
        """Securable for a failover group."""
        return Securable(name, "FAILOVER GROUP")

    @staticmethod
    def replication_group(name: str) -> Securable:
        """Securable for a replication group."""
        return Securable(name, "REPLICATION GROUP")

    @staticmethod
    def external_volume(name: str) -> Securable:
        """Securable for an external volume."""
        return Securable(name, "EXTERNAL VOLUME")

    @staticmethod
    def alert(name: str) -> Securable:
        """Securable for an alert."""
        return Securable(name, "ALERT")

    @staticmethod
    def authentication_policy(name: str) -> Securable:
        """Securable for an authentication policy."""
        return Securable(name, "AUTHENTICATION POLICY")

    @staticmethod
    def data_metric_function(name: str) -> Securable:
        """Securable for a data metric function."""
        return Securable(name, "DATA METRIC FUNCTION")

    @staticmethod
    def dynamic_table(name: str) -> Securable:
        """Securable for a dynamic table."""
        return Securable(name, "DYNAMIC TABLE")

    @staticmethod
    def event_table(name: str) -> Securable:
        """Securable for an event table."""
        return Securable(name, "EVENT TABLE")

    @staticmethod
    def external_table(name: str) -> Securable:
        """Securable for an external table."""
        return Securable(name, "EXTERNAL TABLE")

    @staticmethod
    def file_format(name: str) -> Securable:
        """Securable for a file format."""
        return Securable(name, "FILE FORMAT")

    @staticmethod
    def function(name: str) -> Securable:
        """Securable for a function."""
        return Securable(name, "FUNCTION")

    @staticmethod
    def git_repository(name: str) -> Securable:
        """Securable for a git repository."""
        return Securable(name, "GIT REPOSITORY")

    @staticmethod
    def hybrid_table(name: str) -> Securable:
        """Securable for a hybrid table."""
        return Securable(name, "HYBRID TABLE")

    @staticmethod
    def image_repository(name: str) -> Securable:
        """Securable for an image repository."""
        return Securable(name, "IMAGE REPOSITORY")

    @staticmethod
    def iceberg_table(name: str) -> Securable:
        """Securable for an iceberg table."""
        return Securable(name, "ICEBERG TABLE")

    @staticmethod
    def masking_policy(name: str) -> Securable:
        """Securable for a masking policy."""
        return Securable(name, "MASKING POLICY")

    @staticmethod
    def materialized_view(name: str) -> Securable:
        """Securable for a materialized view."""
        return Securable(name, "MATERIALIZED VIEW")

    @staticmethod
    def model(name: str) -> Securable:
        """Securable for a model."""
        return Securable(name, "MODEL")

    @staticmethod
    def network_rule(name: str) -> Securable:
        """Securable for a network rule."""
        return Securable(name, "NETWORK RULE")

    @staticmethod
    def packages_policy(name: str) -> Securable:
        """Securable for a packages policy."""
        return Securable(name, "PACKAGES POLICY")

    @staticmethod
    def password_policy(name: str) -> Securable:
        """Securable for a password policy."""
        return Securable(name, "PASSWORD POLICY")

    @staticmethod
    def pipe(name: str) -> Securable:
        """Securable for a pipe."""
        return Securable(name, "PIPE")

    @staticmethod
    def procedure(name: str) -> Securable:
        """Securable for a procedure."""
        return Securable(name, "PROCEDURE")

    @staticmethod
    def projection_policy(name: str) -> Securable:
        """Securable for a projection policy."""
        return Securable(name, "PROJECTION POLICY")

    @staticmethod
    def row_access_policy(name: str) -> Securable:
        """Securable for a row access policy."""
        return Securable(name, "ROW ACCESS POLICY")

    @staticmethod
    def secret(name: str) -> Securable:
        """Securable for a secret."""
        return Securable(name, "SECRET")

    @staticmethod
    def service(name: str) -> Securable:
        """Securable for a service."""
        return Securable(name, "SERVICE")

    @staticmethod
    def session_policy(name: str) -> Securable:
        """Securable for a session policy."""
        return Securable(name, "SESSION POLICY")

    @staticmethod
    def sequence(name: str) -> Securable:
        """Securable for a sequence."""
        return Securable(name, "SEQUENCE")

    @staticmethod
    def stage(name: str) -> Securable:
        """Securable for a stage."""
        return Securable(name, "STAGE")

    @staticmethod
    def stream(name: str) -> Securable:
        """Securable for a stream."""
        return Securable(name, "STREAM")

    @staticmethod
    def table(name: str) -> Securable:
        """Securable for a table."""
        return Securable(name, "TABLE")

    @staticmethod
    def tag(name: str) -> Securable:
        """Securable for a tag."""
        return Securable(name, "TAG")

    @staticmethod
    def task(name: str) -> Securable:
        """Securable for a task."""
        return Securable(name, "TASK")

    @staticmethod
    def view(name: str) -> Securable:
        """Securable for a view."""
        return Securable(name, "VIEW")

    @staticmethod
    def streamlit(name: str) -> Securable:
        """Securable for a streamlit."""
        return Securable(name, "STREAMLIT")

    @staticmethod
    def notebook(name: str) -> Securable:
        """Securable for a notebook."""
        return Securable(name, "NOTEBOOK")

    @staticmethod
    def all_aggregation_policies(scope: Securable) -> Securable:
        """Securable for all aggregation policies."""
        return Securable("ALL", "AGGREGATION POLICIES", scope)

    @staticmethod
    def all_alerts(scope: Securable) -> Securable:
        """Securable for all alerts."""
        return Securable("ALL", "ALERTS", scope)

    @staticmethod
    def all_authentication_policies(scope: Securable) -> Securable:
        """Securable for all authentication policies."""
        return Securable("ALL", "AUTHENTICATION POLICIES", scope)

    @staticmethod
    def all_data_metric_functions(scope: Securable) -> Securable:
        """Securable for all data metric functions."""
        return Securable("ALL", "DATA METRIC FUNCTIONS", scope)

    @staticmethod
    def all_dynamic_tables(scope: Securable) -> Securable:
        """Securable for all dynamic tables."""
        return Securable("ALL", "DYNAMIC TABLES", scope)

    @staticmethod
    def all_event_tables(scope: Securable) -> Securable:
        """Securable for all event tables."""
        return Securable("ALL", "EVENT TABLES", scope)

    @staticmethod
    def all_external_tables(scope: Securable) -> Securable:
        """Securable for all external tables."""
        return Securable("ALL", "EXTERNAL TABLES", scope)

    @staticmethod
    def all_file_formats(scope: Securable) -> Securable:
        """Securable for all file formats."""
        return Securable("ALL", "FILE FORMATS", scope)

    @staticmethod
    def all_functions(scope: Securable) -> Securable:
        """Securable for all functions."""
        return Securable("ALL", "FUNCTIONS", scope)

    @staticmethod
    def all_git_repositories(scope: Securable) -> Securable:
        """Securable for all git repositories."""
        return Securable("ALL", "GIT REPOSITORIES", scope)

    @staticmethod
    def all_hybrid_tables(scope: Securable) -> Securable:
        """Securable for all hybrid tables."""
        return Securable("ALL", "HYBRID TABLES", scope)

    @staticmethod
    def all_image_repositories(scope: Securable) -> Securable:
        """Securable for all image repositories."""
        return Securable("ALL", "IMAGE REPOSITORIES", scope)

    @staticmethod
    def all_iceberg_tables(scope: Securable) -> Securable:
        """Securable for all iceberg tables."""
        return Securable("ALL", "ICEBERG TABLES", scope)

    @staticmethod
    def all_masking_policies(scope: Securable) -> Securable:
        """Securable for all masking policies."""
        return Securable("ALL", "MASKING POLICIES", scope)

    @staticmethod
    def all_materialized_views(scope: Securable) -> Securable:
        """Securable for all materialized views."""
        return Securable("ALL", "MATERIALIZED VIEWS", scope)

    @staticmethod
    def all_models(scope: Securable) -> Securable:
        """Securable for all models."""
        return Securable("ALL", "MODELS", scope)

    @staticmethod
    def all_network_rules(scope: Securable) -> Securable:
        """Securable for all network rules."""
        return Securable("ALL", "NETWORK RULES", scope)

    @staticmethod
    def all_packages_policies(scope: Securable) -> Securable:
        """Securable for all packages policies."""
        return Securable("ALL", "PACKAGES POLICIES", scope)

    @staticmethod
    def all_password_policies(scope: Securable) -> Securable:
        """Securable for all password policies."""
        return Securable("ALL", "PASSWORD POLICIES", scope)

    @staticmethod
    def all_procedures(scope: Securable) -> Securable:
        """Securable for all procedures."""
        return Securable("ALL", "PROCEDURES", scope)

    @staticmethod
    def all_projection_policies(scope: Securable) -> Securable:
        """Securable for all projection policies."""
        return Securable("ALL", "PROJECTION POLICIES", scope)

    @staticmethod
    def all_row_access_policies(scope: Securable) -> Securable:
        """Securable for all row access policies."""
        return Securable("ALL", "ROW ACCESS POLICIES", scope)

    @staticmethod
    def all_schemas(scope: Securable) -> Securable:
        """Securable for all schemas."""
        return Securable("ALL", "SCHEMAS", scope)

    @staticmethod
    def all_secrets(scope: Securable) -> Securable:
        """Securable for all secrets."""
        return Securable("ALL", "SECRETS", scope)

    @staticmethod
    def all_services(scope: Securable) -> Securable:
        """Securable for all services."""
        return Securable("ALL", "SERVICES", scope)

    @staticmethod
    def all_session_policies(scope: Securable) -> Securable:
        """Securable for all session policies."""
        return Securable("ALL", "SESSION POLICIES", scope)

    @staticmethod
    def all_sequences(scope: Securable) -> Securable:
        """Securable for all sequences."""
        return Securable("ALL", "SEQUENCES", scope)

    @staticmethod
    def all_stages(scope: Securable) -> Securable:
        """Securable for all stages."""
        return Securable("ALL", "STAGES", scope)

    @staticmethod
    def all_streams(scope: Securable) -> Securable:
        """Securable for all streams."""
        return Securable("ALL", "STREAMS", scope)

    @staticmethod
    def all_tables(scope: Securable) -> Securable:
        """Securable for all tables."""
        return Securable("ALL", "TABLES", scope)

    @staticmethod
    def all_tags(scope: Securable) -> Securable:
        """Securable for all tags."""
        return Securable("ALL", "TAGS", scope)

    @staticmethod
    def all_tasks(scope: Securable) -> Securable:
        """Securable for all tasks."""
        return Securable("ALL", "TASKS", scope)

    @staticmethod
    def all_views(scope: Securable) -> Securable:
        """Securable for all views."""
        return Securable("ALL", "VIEWS", scope)

    @staticmethod
    def all_streamlits(scope: Securable) -> Securable:
        """Securable for all streamlits."""
        return Securable("ALL", "STREAMLITS", scope)

    @staticmethod
    def all_notebooks(scope: Securable) -> Securable:
        """Securable for all notebooks."""
        return Securable("ALL", "NOTEBOOKS", scope)

    @staticmethod
    def future_aggregation_policies(scope: Securable) -> Securable:
        """Securable for future aggregation policies."""
        return Securable("FUTURE", "AGGREGATION POLICIES", scope)

    @staticmethod
    def future_alerts(scope: Securable) -> Securable:
        """Securable for future alerts."""
        return Securable("FUTURE", "ALERTS", scope)

    @staticmethod
    def future_authentication_policies(scope: Securable) -> Securable:
        """Securable for future authentication policies."""
        return Securable("FUTURE", "AUTHENTICATION POLICIES", scope)

    @staticmethod
    def future_data_metric_functions(scope: Securable) -> Securable:
        """Securable for future data metric functions."""
        return Securable("FUTURE", "DATA METRIC FUNCTIONS", scope)

    @staticmethod
    def future_dynamic_tables(scope: Securable) -> Securable:
        """Securable for future dynamic tables."""
        return Securable("FUTURE", "DYNAMIC TABLES", scope)

    @staticmethod
    def future_event_tables(scope: Securable) -> Securable:
        """Securable for future event tables."""
        return Securable("FUTURE", "EVENT TABLES", scope)

    @staticmethod
    def future_external_tables(scope: Securable) -> Securable:
        """Securable for future external tables."""
        return Securable("FUTURE", "EXTERNAL TABLES", scope)

    @staticmethod
    def future_file_formats(scope: Securable) -> Securable:
        """Securable for future file formats."""
        return Securable("FUTURE", "FILE FORMATS", scope)

    @staticmethod
    def future_functions(scope: Securable) -> Securable:
        """Securable for future functions."""
        return Securable("FUTURE", "FUNCTIONS", scope)

    @staticmethod
    def future_git_repositories(scope: Securable) -> Securable:
        """Securable for future git repositories."""
        return Securable("FUTURE", "GIT REPOSITORIES", scope)

    @staticmethod
    def future_hybrid_tables(scope: Securable) -> Securable:
        """Securable for future hybrid tables."""
        return Securable("FUTURE", "HYBRID TABLES", scope)

    @staticmethod
    def future_image_repositories(scope: Securable) -> Securable:
        """Securable for future image repositories."""
        return Securable("FUTURE", "IMAGE REPOSITORIES", scope)

    @staticmethod
    def future_iceberg_tables(scope: Securable) -> Securable:
        """Securable for future iceberg tables."""
        return Securable("FUTURE", "ICEBERG TABLES", scope)

    @staticmethod
    def future_masking_policies(scope: Securable) -> Securable:
        """Securable for future masking policies."""
        return Securable("FUTURE", "MASKING POLICIES", scope)

    @staticmethod
    def future_materialized_views(scope: Securable) -> Securable:
        """Securable for future materialized views."""
        return Securable("FUTURE", "MATERIALIZED VIEWS", scope)

    @staticmethod
    def future_models(scope: Securable) -> Securable:
        """Securable for future models."""
        return Securable("FUTURE", "MODELS", scope)

    @staticmethod
    def future_network_rules(scope: Securable) -> Securable:
        """Securable for future network rules."""
        return Securable("FUTURE", "NETWORK RULES", scope)

    @staticmethod
    def future_packages_policies(scope: Securable) -> Securable:
        """Securable for future packages policies."""
        return Securable("FUTURE", "PACKAGES POLICIES", scope)

    @staticmethod
    def future_password_policies(scope: Securable) -> Securable:
        """Securable for future password policies."""
        return Securable("FUTURE", "PASSWORD POLICIES", scope)

    @staticmethod
    def future_procedures(scope: Securable) -> Securable:
        """Securable for future procedures."""
        return Securable("FUTURE", "PROCEDURES", scope)

    @staticmethod
    def future_projection_policies(scope: Securable) -> Securable:
        """Securable for future projection policies."""
        return Securable("FUTURE", "PROJECTION POLICIES", scope)

    @staticmethod
    def future_row_access_policies(scope: Securable) -> Securable:
        """Securable for future row access policies."""
        return Securable("FUTURE", "ROW ACCESS POLICIES", scope)

    @staticmethod
    def future_schemas(scope: Securable) -> Securable:
        """Securable for future schemas."""
        return Securable("FUTURE", "SCHEMAS", scope)

    @staticmethod
    def future_secrets(scope: Securable) -> Securable:
        """Securable for future secrets."""
        return Securable("FUTURE", "SECRETS", scope)

    @staticmethod
    def future_services(scope: Securable) -> Securable:
        """Securable for future services."""
        return Securable("FUTURE", "SERVICES", scope)

    @staticmethod
    def future_session_policies(scope: Securable) -> Securable:
        """Securable for future session policies."""
        return Securable("FUTURE", "SESSION POLICIES", scope)

    @staticmethod
    def future_sequences(scope: Securable) -> Securable:
        """Securable for future sequences."""
        return Securable("FUTURE", "SEQUENCES", scope)

    @staticmethod
    def future_stages(scope: Securable) -> Securable:
        """Securable for future stages."""
        return Securable("FUTURE", "STAGES", scope)

    @staticmethod
    def future_streams(scope: Securable) -> Securable:
        """Securable for future streams."""
        return Securable("FUTURE", "STREAMS", scope)

    @staticmethod
    def future_tables(scope: Securable) -> Securable:
        """Securable for future tables."""
        return Securable("FUTURE", "TABLES", scope)

    @staticmethod
    def future_tags(scope: Securable) -> Securable:
        """Securable for future tags."""
        return Securable("FUTURE", "TAGS", scope)

    @staticmethod
    def future_tasks(scope: Securable) -> Securable:
        """Securable for future tasks."""
        return Securable("FUTURE", "TASKS", scope)

    @staticmethod
    def future_views(scope: Securable) -> Securable:
        """Securable for future views."""
        return Securable("FUTURE", "VIEWS", scope)

    @staticmethod
    def future_streamlits(scope: Securable) -> Securable:
        """Securable for future streamlits."""
        return Securable("FUTURE", "STREAMLITS", scope)

    @staticmethod
    def future_notebooks(scope: Securable) -> Securable:
        """Securable for future notebooks."""
        return Securable("FUTURE", "NOTEBOOKS", scope)
