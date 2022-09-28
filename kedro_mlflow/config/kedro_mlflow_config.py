import os
from logging import getLogger
from pathlib import Path, PurePath
from typing import List, Optional
from urllib.parse import urlparse

import mlflow
from kedro.framework.context import KedroContext
from mlflow.entities import Experiment
from mlflow.tracking.client import MlflowClient
from pydantic import BaseModel, PrivateAttr, StrictBool
from typing_extensions import Literal

LOGGER = getLogger(__name__)


class ExpNameNotEnvSet(Exception):
    """Raised if the experiment name \
        is not set as environment variable."""

    pass


class MLflowConfigCredentialsException(Exception):
    """Raised if the credentials needed to \
        configure and create the azure_mlflow_uri \
             are not found."""

    pass


class MlflowServerOptions(BaseModel):
    # mutable default is ok for pydantic :
    #  https://stackoverflow.com/questions/63793662/how-to-give-a-pydantic-list-field-a-default-value
    mlflow_tracking_uri: Optional[str] = None
    credentials: Optional[str] = None
    _mlflow_client: MlflowClient = PrivateAttr()

    class Config:
        extra = "forbid"


class DisableTrackingOptions(BaseModel):
    # mutable default is ok for pydantic :
    #  https://stackoverflow.com/questions/63793662/how-to-give-a-pydantic-list-field-a-default-value
    pipelines: List[str] = []

    class Config:
        extra = "forbid"


class ExperimentOptions(BaseModel):
    name: str = "Default"
    restore_if_deleted: StrictBool = True
    exp_name_environ_var: StrictBool = True
    _experiment: Experiment = PrivateAttr()
    # do not create _experiment immediately to avoid creating
    # a database connection when creating the object
    # it will be instantiated on setup() call

    class Config:
        extra = "forbid"


class RunOptions(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    nested: StrictBool = True

    class Config:
        extra = "forbid"


class DictParamsOptions(BaseModel):
    flatten: StrictBool = False
    recursive: StrictBool = True
    sep: str = "."

    class Config:
        extra = "forbid"


class TagsParamsOptions(BaseModel):
    area: Optional[str] = None
    client: Optional[str] = None
    ds_owner: Optional[str] = None
    mle_owner: Optional[str] = None
    type: Optional[str] = None
    application: Optional[str] = None

    class Config:
        extra = "allow"


class MlflowParamsOptions(BaseModel):
    multitarget: StrictBool = False
    tags: TagsParamsOptions = TagsParamsOptions()
    dict_params: DictParamsOptions = DictParamsOptions()
    long_params_strategy: Literal["fail", "truncate", "tag"] = "fail"

    class Config:
        extra = "forbid"


class MlflowTrackingOptions(BaseModel):
    # mutable default is ok for pydantic :
    # https://stackoverflow.com/questions/63793662/how-to-give-a-pydantic-list-field-a-default-value
    disable_tracking: DisableTrackingOptions = DisableTrackingOptions()
    experiment: ExperimentOptions = ExperimentOptions()
    run: RunOptions = RunOptions()
    params: MlflowParamsOptions = MlflowParamsOptions()

    class Config:
        extra = "forbid"


class UiOptions(BaseModel):

    port: str = "5000"
    host: str = "127.0.0.1"

    class Config:
        extra = "forbid"


class KedroMlflowConfig(BaseModel):
    server: MlflowServerOptions = MlflowServerOptions()
    tracking: MlflowTrackingOptions = MlflowTrackingOptions()
    ui: UiOptions = UiOptions()

    class Config:
        # force triggering type control when setting value instead of init
        validate_assignment = True
        # raise an error if an unknown key is passed to the constructor
        extra = "forbid"

    def setup(self, context):
        """Setup all the mlflow configuration"""

        self.server.mlflow_tracking_uri = self._validate_mlflow_tracking_uri(
            project_path=context.project_path,
            uri=self.server.mlflow_tracking_uri,
            context=context,
        )

        # init after validating the uri, else mlflow creates a mlruns folder at the root
        self.server._mlflow_client = MlflowClient(
            tracking_uri=self.server.mlflow_tracking_uri
        )

        self._export_credentials(context)

        # we set the configuration now: it takes priority
        # if it has already be set in export_credentials
        mlflow.set_tracking_uri(self.server.mlflow_tracking_uri)

        self._set_experiment()

    def _export_credentials(self, context: KedroContext):
        conf_creds = context._get_config_credentials()
        mlflow_creds = conf_creds.get(self.server.credentials, {})
        for key, value in mlflow_creds.items():
            os.environ[key] = value

    def _set_experiment(self):
        """Best effort to get the experiment associated
        to the configuration

        Returns:
            mlflow.entities.Experiment -- [description]
        """

        # we retrieve the experiment manually to check if it exsits
        mlflow_experiment = self.server._mlflow_client.get_experiment_by_name(
            name=self.tracking.experiment.name
        )
        # Deal with two side case when retrieving the experiment
        if mlflow_experiment is not None:
            if (
                self.tracking.experiment.restore_if_deleted
                and mlflow_experiment.lifecycle_stage == "deleted"
            ):
                # the experiment was created, then deleted :
                # we have to restore it manually before setting it as the active one
                self.server._mlflow_client.restore_experiment(
                    mlflow_experiment.experiment_id
                )

        # this creates the experiment if it does not exists
        # and creates a global variable with the experiment
        # but returns nothing
        mlflow.set_experiment(experiment_name=self.tracking.experiment.name)

        # we do not use "experiment" variable directly but we fetch again from the database
        # because if it did not exists at all, it was created by previous command
        self.tracking.experiment._experiment = (
            self.server._mlflow_client.get_experiment_by_name(
                name=self.tracking.experiment.name
            )
        )

    def _validate_mlflow_tracking_uri(
        self, project_path: str, uri: Optional[str], context: KedroContext
    ) -> str:
        """Format the uri provided to match mlflow expectations.

        Arguments:
            uri {Union[None, str]} -- A valid filepath for mlflow uri

        Returns:
            str -- A valid mlflow_tracking_uri
        """

        if uri == "databricks":
            return uri

        elif uri == "azure":
            conf_creds = context._get_config_credentials()
            azure_credentials = conf_creds.get(self.server.credentials, {})

            try:
                subscription_id = azure_credentials["subscription_id"]
                resource_group = azure_credentials["resource_group"]
                workspace_name = azure_credentials["workspace_name"]
                region = azure_credentials["region"]

                azure_mlflow_uri = f"azureml://{region}.api.azureml.ms/mlflow/v1.0/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/Microsoft.MachineLearningServices/workspaces/{workspace_name}"

                return azure_mlflow_uri

            except MLflowConfigCredentialsException:
                LOGGER.warning(
                    "Check the Azure ML credetials in the credentials.yml file."
                )

        else:
            # if uri is None (if no tracking uri is provided), 
            # we register the runs locally at the root of the project)
            # do not use mlflow.get_tracking_uri() because if there is no env var,
            # it resolves to 'Path.cwd() / "mlruns"'
            # but we want 'project_path / "mlruns"'
            # uri = os.environ.get("MLFLOW_TRACKING_URI", "mlruns")

            uri = os.environ.get("MLFLOW_TRACKING_URI", "mlruns")

            pathlib_uri = PurePath(uri)

            if pathlib_uri.is_absolute():
                valid_uri = pathlib_uri.as_uri()
            else:
                parsed = urlparse(uri)
                if parsed.scheme == "":
                    # if it is a local relative path, make it absolute
                    # .resolve() does not work well on windows
                    # .absolute is undocumented and have known bugs
                    # Path.cwd() / uri is the recommend way by core developpers.
                    # See : https://discuss.python.org/t/pathlib-absolute-vs-resolve/2573/6
                    valid_uri = (Path(project_path) / uri).as_uri()
                    LOGGER.info(
                        f"The 'mlflow_tracking_uri' key in mlflow.yml is relative \
                            ('server.mlflow_tracking_uri = {uri}'). \
                                It is converted to a valid uri: '{valid_uri}'"
                    )
                else:
                    # else assume it is an uri
                    valid_uri = uri

            return valid_uri
