import copy
import json
import os
import logging
import re
import boto3
import cfnresponse
from botocore.exceptions import ClientError

import sra_s3
import sra_repo
import sra_ssm_params
import sra_iam
import sra_dynamodb
import sra_sts
import sra_lambda
import sra_sns
import sra_config
import sra_cloudwatch
import sra_kms

from typing import Dict, Any, List

# import sra_lambda

# TODO(liamschn): If dynamoDB sra_state table exists, use it
# TODO(liamschn): Where do we see dry-run data?  Maybe S3 staging bucket file?  The sra_state table? Another DynamoDB table?
# TODO(liamschn): deploy example bedrock guardrail
# TODO(liamschn): deploy example iam role(s) and policy(ies) - lower priority/not necessary?
# TODO(liamschn): deploy example bucket policy(ies) - lower priority/not necessary?
# TODO(liamschn): deal with linting failures in pipeline
# TODO(liamschn): deal with typechecking/mypy
# TODO(liamschn): check for unused parameters
# TODO(liamschn): need to ensure DRY_RUN is false for any dynamodb state table record insertions

from typing import TYPE_CHECKING, Sequence  # , Union, Literal, Optional

if TYPE_CHECKING:
    from mypy_boto3_ssm.type_defs import TagTypeDef

LOGGER = logging.getLogger(__name__)
log_level: str = os.environ.get("LOG_LEVEL", "INFO")
LOGGER.setLevel(log_level)


# TODO(liamschn): change this so that it downloads the sra_config_lambda_iam_permissions.json from the repo then loads into the IAM_POLICY_DOCUMENTS variable (make this step 2 in the create function below)
def load_iam_policy_documents() -> Dict[str, Any]:
    json_file_path = os.path.join(os.path.dirname(__file__), "sra_config_lambda_iam_permissions.json")
    with open(json_file_path, "r") as file:
        return json.load(file)


def load_cloudwatch_metric_filters() -> dict:
    with open("sra_cloudwatch_metric_filters.json", "r") as file:
        return json.load(file)


def load_kms_key_policies() -> dict:
    with open("sra_kms_keys.json", "r") as file:
        return json.load(file)


def load_cloudwatch_oam_sink_policy() -> dict:
    with open("sra_cloudwatch_oam_sink_policy.json", "r") as file:
        return json.load(file)


def load_sra_cloudwatch_oam_trust_policy() -> dict:
    with open("sra_cloudwatch_oam_trust_policy.json", "r") as file:
        return json.load(file)

def load_sra_cloudwatch_dashboard() -> dict:
    with open("sra_cloudwatch_dashboard.json", "r") as file:
        return json.load(file)

# Global vars
RESOURCE_TYPE: str = ""
STATE_TABLE: str = "sra_state"
SOLUTION_NAME: str = "sra-bedrock-org"
GOVERNED_REGIONS = []
SECURITY_ACCOUNT = ""
ORGANIZATION_ID = ""
SRA_ALARM_EMAIL: str = ""
SRA_ALARM_TOPIC_ARN: str = ""
STATE_TABLE: str = "sra_state" # for saving resource info

LAMBDA_START: str = ""
LAMBDA_FINISH: str = ""

ACCOUNT: str = boto3.client("sts").get_caller_identity().get("Account")
REGION: str = os.environ.get("AWS_REGION")
CFN_RESOURCE_ID: str = "sra-bedrock-org-function"
ALARM_SNS_KEY_ALIAS = "sra-alarm-sns-key"

# CFN_RESPONSE_DATA definition:
#   dry_run: bool - type of run
#   deployment_info: dict - information about the deployment
#       action_count: int - number of actions taken
#       resources_deployed: int - number of resources deployed
#       configuration_changes: int - number of configuration changes
CFN_RESPONSE_DATA: dict = {"dry_run": True, "deployment_info": {"action_count": 0, "resources_deployed": 0, "configuration_changes": 0}}
# TODO(liamschn): Consider adding "regions_targeted": int and "accounts_targeted": in to "deployment_info" of CFN_RESPONSE_DATA


# dry run global variables
DRY_RUN: bool = True
DRY_RUN_DATA: dict = {}

# other global variables
LIVE_RUN_DATA: dict = {}
IAM_POLICY_DOCUMENTS: Dict[str, Any] = load_iam_policy_documents()
CLOUDWATCH_METRIC_FILTERS: dict = load_cloudwatch_metric_filters()
KMS_KEY_POLICIES: dict = load_kms_key_policies()
CLOUDWATCH_OAM_SINK_POLICY: dict = load_cloudwatch_oam_sink_policy()
CLOUDWATCH_OAM_TRUST_POLICY: dict = load_sra_cloudwatch_oam_trust_policy()
CLOUDWATCH_DASHBOARD: dict = load_sra_cloudwatch_dashboard()

# Parameter validation rules
PARAMETER_VALIDATION_RULES: dict = {
    "SRA_REPO_ZIP_URL": r'^https://.*\.zip$',
    "DRY_RUN": r'^true|false$',
    "EXECUTION_ROLE_NAME": r'^sra-execution$',
    "LOG_GROUP_DEPLOY": r'^true|false$',
    "LOG_GROUP_RETENTION": r'^(1|3|5|7|14|30|60|90|120|150|180|365|400|545|731|1096|1827|2192|2557|2922|3288|3653)$',
    "LOG_LEVEL": r'^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$',
    "SOLUTION_NAME": r'^sra-bedrock-org$',
    "SOLUTION_VERSION": r'^[0-9]+\.[0-9]+\.[0-9]+$',
    "SRA_ALARM_EMAIL": r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$',
    "SRA-BEDROCK-ACCOUNTS": r'^\[((?:"[0-9]+"(?:\s*,\s*)?)*)\]$',
    "SRA-BEDROCK-REGIONS": r'^\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\]$',
    "SRA-BEDROCK-CHECK-EVAL-JOB-BUCKET": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"input_params"\s*:\s*(\{\s*(?:"BucketName"\s*:\s*"([a-zA-Z0-9-]*)"\s*)?})\}$',
    "SRA-BEDROCK-CHECK-IAM-USER-ACCESS": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"input_params"\s*:\s*(\{\s*(?:"BucketName"\s*:\s*"([a-zA-Z0-9-]*)"\s*)?})\}$',
    "SRA-BEDROCK-CHECK-GUARDRAILS": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"input_params"\s*:\s*\{(\s*"content_filters"\s*:\s*"(true|false)")?(\s*,\s*"denied_topics"\s*:\s*"(true|false)")?(\s*,\s*"word_filters"\s*:\s*"(true|false)")?(\s*,\s*"sensitive_info_filters"\s*:\s*"(true|false)")?(\s*,\s*"contextual_grounding"\s*:\s*"(true|false)")?\s*\}\}$',
    "SRA-BEDROCK-CHECK-VPC-ENDPOINTS": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"input_params"\s*:\s*\{(\s*"check_bedrock"\s*:\s*"(true|false)")?(\s*,\s*"check_bedrock_agent"\s*:\s*"(true|false)")?(\s*,\s*"check_bedrock_agent_runtime"\s*:\s*"(true|false)")?(\s*,\s*"check_bedrock_runtime"\s*:\s*"(true|false)")?\s*\}\}$',
    "SRA-BEDROCK-CHECK-INVOCATION-LOG-CLOUDWATCH": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"input_params"\s*:\s*\{(\s*"check_retention"\s*:\s*"(true|false)")?(\s*,\s*"check_encryption"\s*:\s*"(true|false)")?\}\}$',
    "SRA-BEDROCK-CHECK-INVOCATION-LOG-S3": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"input_params"\s*:\s*\{(\s*"check_retention"\s*:\s*"(true|false)")?(\s*,\s*"check_encryption"\s*:\s*"(true|false)")?(\s*,\s*"check_access_logging"\s*:\s*"(true|false)")?(\s*,\s*"check_object_locking"\s*:\s*"(true|false)")?(\s*,\s*"check_versioning"\s*:\s*"(true|false)")?\s*\}\}$',
    "SRA-BEDROCK-CHECK-CLOUDWATCH-ENDPOINTS": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"input_params"\s*:\s*(\{\})\}$',
    "SRA-BEDROCK-CHECK-S3-ENDPOINTS": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"input_params"\s*:\s*(\{\})\}$',
    "SRA-BEDROCK-CHECK-GUARDRAIL-ENCRYPTION": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"input_params"\s*:\s*(\{\})\}$',
    "SRA-BEDROCK-FILTER-SERVICE-CHANGES": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"filter_params"\s*:\s*\{"log_group_name"\s*:\s*"[^"\s]+"\}\}$',
    "SRA-BEDROCK-FILTER-BUCKET-CHANGES": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"filter_params"\s*:\s*\{"log_group_name"\s*:\s*"[^"\s]+",\s*"bucket_names"\s*:\s*\[((?:"[^"\s]+"(?:\s*,\s*)?)+)\]\}\}$',
    "SRA-BEDROCK-FILTER-PROMPT-INJECTION": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"filter_params"\s*:\s*\{"log_group_name"\s*:\s*"[^"\s]+",\s*"input_path"\s*:\s*"[^"\s]+"\}\}$',
    "SRA-BEDROCK-FILTER-SENSITIVE-INFO": r'^\{"deploy"\s*:\s*"(true|false)",\s*"accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\],\s*"filter_params"\s*:\s*\{"log_group_name"\s*:\s*"[^"\s]+",\s*"input_path"\s*:\s*"[^"\s]+"\}\}$',
    "SRA-BEDROCK-CENTRAL-OBSERVABILITY": r'^\{"deploy"\s*:\s*"(true|false)",\s*"bedrock_accounts"\s*:\s*\[((?:"[0-9]+"(?:\s*,\s*)?)*)\],\s*"regions"\s*:\s*\[((?:"[a-z0-9-]+"(?:\s*,\s*)?)*)\]\}$',
}

# Instantiate sra class objects
# todo(liamschn): can these files exist in some central location to be shared with other solutions?
ssm_params = sra_ssm_params.sra_ssm_params()
iam = sra_iam.sra_iam()
dynamodb = sra_dynamodb.sra_dynamodb()
sts = sra_sts.sra_sts()
repo = sra_repo.sra_repo()
s3 = sra_s3.sra_s3()
lambdas = sra_lambda.sra_lambda()
sns = sra_sns.sra_sns()
config = sra_config.sra_config()
cloudwatch = sra_cloudwatch.sra_cloudwatch()
kms = sra_kms.sra_kms()

# propagate solution name to class objects
cloudwatch.SOLUTION_NAME = SOLUTION_NAME

def get_resource_parameters(event):
    global DRY_RUN
    global GOVERNED_REGIONS
    global CFN_RESPONSE_DATA
    global SRA_ALARM_EMAIL
    global SECURITY_ACCOUNT
    global ORGANIZATION_ID

    param_validation: dict = validate_parameters(event["ResourceProperties"], PARAMETER_VALIDATION_RULES)
    if param_validation["success"] is False:
        LOGGER.info(f"Parameter validation failed: {param_validation['errors']}")
        raise ValueError(f"Parameter validation failed: {param_validation['errors']}") from None
    else:
        LOGGER.info("Parameter validation succeeded")

    LOGGER.info("Getting resource params...")
    repo.REPO_ZIP_URL = event["ResourceProperties"]["SRA_REPO_ZIP_URL"]
    repo.REPO_BRANCH = repo.REPO_ZIP_URL.split(".")[1].split("/")[len(repo.REPO_ZIP_URL.split(".")[1].split("/")) - 1]
    repo.SOLUTIONS_DIR = f"/tmp/aws-security-reference-architecture-examples-{repo.REPO_BRANCH}/aws_sra_examples/solutions"

    sts.CONFIGURATION_ROLE = "sra-execution"
    governed_regions_param = ssm_params.get_ssm_parameter(
        ssm_params.MANAGEMENT_ACCOUNT_SESSION, REGION, "/sra/regions/customer-control-tower-regions"
    )
    if governed_regions_param[0] is True:
        GOVERNED_REGIONS = governed_regions_param[1]
        LOGGER.info(f"Successfully retrieved the SRA governed regions parameter: {GOVERNED_REGIONS}")
    else:
        LOGGER.info("Error retrieving SRA governed regions ssm parameter.  Is the SRA common prerequisites solution deployed?")
        raise ValueError("Error retrieving SRA governed regions ssm parameter.  Is the SRA common prerequisites solution deployed?") from None

    security_acct_param = ssm_params.get_ssm_parameter(ssm_params.MANAGEMENT_ACCOUNT_SESSION, REGION, "/sra/control-tower/audit-account-id")
    if security_acct_param[0] is True:
        SECURITY_ACCOUNT = security_acct_param[1] # TODO(liamschn): switch to using the class SRA_SECURITY_ACCT variable?
        ssm_params.SRA_SECURITY_ACCT = security_acct_param[1]
        LOGGER.info(f"Successfully retrieved the SRA security account parameter: {SECURITY_ACCOUNT}")
    else:
        LOGGER.info("Error retrieving SRA security account ssm parameter.  Is the SRA common prerequisites solution deployed?")
        raise ValueError("Error retrieving SRA security account ssm parameter.  Is the SRA common prerequisites solution deployed?") from None

    org_id_param = ssm_params.get_ssm_parameter(ssm_params.MANAGEMENT_ACCOUNT_SESSION, REGION, "/sra/control-tower/organization-id")
    if org_id_param[0] is True:
        ORGANIZATION_ID = org_id_param[1]
        LOGGER.info(f"Successfully retrieved the SRA organization id parameter: {ORGANIZATION_ID}")
    else:
        LOGGER.info("Error retrieving SRA organization id ssm parameter.  Is the SRA common prerequisites solution deployed?")
        raise ValueError("Error retrieving SRA organization id ssm parameter.  Is the SRA common prerequisites solution deployed?") from None

    staging_bucket_param = ssm_params.get_ssm_parameter(ssm_params.MANAGEMENT_ACCOUNT_SESSION, REGION, "/sra/staging-s3-bucket-name")
    if staging_bucket_param[0] is True:
        s3.STAGING_BUCKET = staging_bucket_param[1]
        LOGGER.info(f"Successfully retrieved the SRA staging bucket parameter: {s3.STAGING_BUCKET}")
    else:
        LOGGER.info("Error retrieving SRA staging bucket ssm parameter.  Is the SRA common prerequisites solution deployed?")
        raise ValueError("Error retrieving SRA staging bucket ssm parameter.  Is the SRA common prerequisites solution deployed?") from None

    if event["ResourceProperties"]["SRA_ALARM_EMAIL"] != "":
        SRA_ALARM_EMAIL = event["ResourceProperties"]["SRA_ALARM_EMAIL"]

    if event["ResourceProperties"]["DRY_RUN"] == "true":
        # dry run
        LOGGER.info("Dry run enabled...")
        DRY_RUN = True
    else:
        # live run
        LOGGER.info("Dry run disabled...")
        DRY_RUN = False
    CFN_RESPONSE_DATA["dry_run"] = DRY_RUN


def validate_parameters(parameters: Dict[str, str], rules: Dict[str, str]) -> Dict[str, object]:
    """Validates each parameter against its corresponding regular expression.

    Args:
        parameters (Dict[str, str]): Dictionary of parameters to validate
        rules (Dict[str, str]): Dictionary of parameter names and regex patterns

    Returns:
        Dict[str, object]: Dictionary with 'success' key (bool) and 'errors' key (list of error messages)
    """
    errors: List[str] = []
    
    for param, regex in rules.items():
        value = parameters.get(param)
        if value is None:
            errors.append(f"Parameter '{param}' is missing.")
        elif not re.match(regex, value):
            errors.append(f"Parameter '{param}' with value '{value}' does not match the expected pattern '{regex}'.")

    return {
        "success": len(errors) == 0,
        "errors": errors
    }


def get_accounts_and_regions(resource_properties):
    """Get accounts and regions from event and return them in a tuple

    Args:
        resource_properties (dict): lambda event resource properties

    Returns:
        tuple: (accounts, rule_regions)
            accounts (list): list of accounts to deploy the rule to
            regions (list): list of regions to deploy the rule to
    """
    accounts = []
    regions = []
    if "SRA-BEDROCK-ACCOUNTS" in resource_properties:
        LOGGER.info("SRA-BEDROCK-ACCOUNTS found in event ResourceProperties")
        accounts = json.loads(resource_properties["SRA-BEDROCK-ACCOUNTS"])
        LOGGER.info(f"SRA-BEDROCK-ACCOUNTS: {accounts}")
    else:
        LOGGER.info("SRA-BEDROCK-ACCOUNTS not found in event ResourceProperties; setting to None and deploy to False")
        accounts = []
    if "SRA-BEDROCK-REGIONS" in resource_properties:
        LOGGER.info("SRA-BEDROCK-REGIONS found in event ResourceProperties")
        regions = json.loads(resource_properties["SRA-BEDROCK-REGIONS"])
        LOGGER.info(f"SRA-BEDROCK-REGIONS: {regions}")
    else:
        LOGGER.info("SRA-BEDROCK-REGIONS not found in event ResourceProperties; setting to None and deploy to False")
        regions = []
    return accounts, regions

def get_rule_params(rule_name, resource_properties):
    """Get rule parameters from event and return them in a tuple

    Args:
        rule_name (str): name of config rule
        resource_properties (dict): lambda event resource properties

    Returns:
        tuple: (rule_deploy, rule_accounts, rule_regions, rule_params)
            rule_deploy (bool): whether to deploy the rule
            rule_accounts (list): list of accounts to deploy the rule to
            rule_regions (list): list of regions to deploy the rule to
            rule_input_params (dict): dictionary of rule input parameters
    """
            #     rule_accounts (list): list of accounts to deploy the rule to
            # rule_regions (list): list of regions to deploy the rule to

    if rule_name.upper() in resource_properties:
        LOGGER.info(f"{rule_name} parameter found in event ResourceProperties")
        rule_params = json.loads(resource_properties[rule_name.upper()])
        LOGGER.info(f"{rule_name.upper()} parameters: {rule_params}")
        if "deploy" in rule_params:
            LOGGER.info(f"{rule_name.upper()} 'deploy' parameter found in event ResourceProperties")
            if rule_params["deploy"] == "true":
                LOGGER.info(f"{rule_name.upper()} 'deploy' parameter set to 'true'")
                rule_deploy = True
            else:
                LOGGER.info(f"{rule_name.upper()} 'deploy' parameter set to 'false'")
                rule_deploy = False
        else:
            LOGGER.info(f"{rule_name.upper()} 'deploy' parameter not found in event ResourceProperties; setting to False")
            rule_deploy = False
        if "accounts" in rule_params:
            LOGGER.info(f"{rule_name.upper()} 'accounts' parameter found in event ResourceProperties")
            rule_accounts = rule_params["accounts"]
            LOGGER.info(f"{rule_name.upper()} accounts: {rule_accounts}")
        else:
            LOGGER.info(f"{rule_name.upper()} 'accounts' parameter not found in event ResourceProperties; setting to None and deploy to False")
            rule_accounts = []
            rule_deploy = False
        if "regions" in rule_params:
            LOGGER.info(f"{rule_name.upper()} 'regions' parameter found in event ResourceProperties")
            rule_regions = rule_params["regions"]
            LOGGER.info(f"{rule_name.upper()} regions: {rule_regions}")
        else:
            LOGGER.info(f"{rule_name.upper()} 'regions' parameter not found in event ResourceProperties; setting to None and deploy to False")
            rule_regions = []
            rule_deploy = False
        if "input_params" in rule_params:
            LOGGER.info(f"{rule_name.upper()} 'input_params' parameter found in event ResourceProperties")
            rule_input_params = rule_params["input_params"]
            LOGGER.info(f"{rule_name.upper()} input_params: {rule_input_params}")
        else:
            LOGGER.info(f"{rule_name.upper()} 'input_params' parameter not found in event ResourceProperties; setting to None")
            rule_input_params = {}
        return rule_deploy, rule_accounts, rule_regions, rule_input_params
    else:
        LOGGER.info(f"{rule_name.upper()} config rule parameter not found in event ResourceProperties; skipping...")
        return False, [], [], {}


def get_filter_params(filter_name, resource_properties):
    """Get filter parameters from event resource_properties and return them in a tuple

    Args:
        filter_name (str): name of cloudwatch filter
        resource_properties (dict): lambda event ResourceProperties

    Returns:
        tuple: (filter_deploy, filter_pattern)
            filter_deploy (bool): whether to deploy the filter
            filter_accounts (list): list of accounts to deploy the filter to
            filter_regions (list): list of regions to deploy the filter to
            filter_params (dict): dictionary of filter parameters
    """
    if filter_name.upper() in resource_properties:
        LOGGER.info(f"{filter_name} parameter found in event ResourceProperties")
        metric_filter_params = json.loads(resource_properties[filter_name.upper()])
        LOGGER.info(f"{filter_name.upper()} metric filter parameters: {metric_filter_params}")
        if "deploy" in metric_filter_params:
            LOGGER.info(f"{filter_name.upper()} 'deploy' parameter found in event ResourceProperties")
            if metric_filter_params["deploy"] == "true":
                LOGGER.info(f"{filter_name.upper()} 'deploy' parameter set to 'true'")
                filter_deploy = True
            else:
                LOGGER.info(f"{filter_name.upper()} 'deploy' parameter set to 'false'")
                filter_deploy = False
        else:
            LOGGER.info(f"{filter_name.upper()} 'deploy' parameter not found in event ResourceProperties; setting to False")
            filter_deploy = False
        if "accounts" in metric_filter_params:
            LOGGER.info(f"{filter_name.upper()} 'accounts' parameter found in event ResourceProperties")
            filter_accounts = metric_filter_params["accounts"]
            LOGGER.info(f"{filter_name.upper()} accounts: {filter_accounts}")
        else:
            LOGGER.info(f"{filter_name.upper()} 'accounts' parameter not found in event ResourceProperties")
            filter_accounts = []
        if "regions" in metric_filter_params:
            LOGGER.info(f"{filter_name.upper()} 'regions' parameter found in event ResourceProperties")
            filter_regions = metric_filter_params["regions"]
            LOGGER.info(f"{filter_name.upper()} regions: {filter_regions}")
        else:
            LOGGER.info(f"{filter_name.upper()} 'regions' parameter not found in event ResourceProperties")
            filter_regions = []
        if "filter_params" in metric_filter_params:
            LOGGER.info(f"{filter_name.upper()} 'filter_params' parameter found in event ResourceProperties")
            filter_params = metric_filter_params["filter_params"]
            LOGGER.info(f"{filter_name.upper()} filter_params: {filter_params}")
        else:
            LOGGER.info(f"{filter_name.upper()} 'filter_params' parameter not found in event ResourceProperties")
            filter_params = {}
    else:
        LOGGER.info(f"{filter_name.upper()} filter parameter not found in event ResourceProperties; skipping...")
        return False, [], [], {}
    return filter_deploy, filter_accounts, filter_regions, filter_params


def build_s3_metric_filter_pattern(bucket_names: list, filter_pattern_template: str) -> str:
    # Get the S3 filter
    s3_filter = filter_pattern_template

    # If multiple bucket names are provided, create an OR condition
    if len(bucket_names) > 1:
        bucket_condition = " || ".join([f'$.requestParameters.bucketName = "{bucket}"' for bucket in bucket_names])
        s3_filter = s3_filter.replace('($.requestParameters.bucketName = "<BUCKET_NAME_PLACEHOLDER>")', f"({bucket_condition})")
    elif len(bucket_names) == 1:
        s3_filter = s3_filter.replace("<BUCKET_NAME_PLACEHOLDER>", bucket_names[0])
    else:
        # If no bucket names are provided, remove the bucket condition entirely
        s3_filter = s3_filter.replace('&& ($.requestParameters.bucketName = "<BUCKET_NAME_PLACEHOLDER>")', "")
    return s3_filter

def build_cloudwatch_dashboard(dashboard_template, solution, bedrock_accounts, regions):
    i = 0
    for bedrock_account in bedrock_accounts:
        for region in regions:
            if i == 0:
                injection_template = copy.deepcopy(dashboard_template[solution]["widgets"][0]["properties"]["metrics"][2])
                sensitive_info_template = copy.deepcopy(dashboard_template[solution]["widgets"][0]["properties"]["metrics"][3])
            else:
                dashboard_template[solution]["widgets"][0]["properties"]["metrics"].append(copy.deepcopy(injection_template))
                dashboard_template[solution]["widgets"][0]["properties"]["metrics"].append(copy.deepcopy(sensitive_info_template))
            dashboard_template[solution]["widgets"][0]["properties"]["metrics"][2 + i][2]["accountId"] = bedrock_account
            dashboard_template[solution]["widgets"][0]["properties"]["metrics"][2 + i][2]["region"] = region
            dashboard_template[solution]["widgets"][0]["properties"]["metrics"][3 + i][2]["accountId"] = bedrock_account
            dashboard_template[solution]["widgets"][0]["properties"]["metrics"][3 + i][2]["region"] = region
            i += 2
    dashboard_template[solution]["widgets"][0]["properties"]["metrics"][0][2]["accountId"] = sts.MANAGEMENT_ACCOUNT
    dashboard_template[solution]["widgets"][0]["properties"]["metrics"][0][2]["region"] = sts.HOME_REGION
    dashboard_template[solution]["widgets"][0]["properties"]["metrics"][1][2]["accountId"] = sts.MANAGEMENT_ACCOUNT
    dashboard_template[solution]["widgets"][0]["properties"]["metrics"][1][2]["region"] = sts.HOME_REGION
    dashboard_template[solution]["widgets"][0]["properties"]["region"] = sts.HOME_REGION
    return dashboard_template[solution]


def deploy_state_table():
    global DRY_RUN_DATA
    global LIVE_RUN_DATA
    global CFN_RESPONSE_DATA

    if DRY_RUN is False:
        LOGGER.info("Live run: creating the state table...")
        # TODO(liamschn): move dynamodb client and resource to the dynamo class object/module
        # TODO(liamschn): move the deploy state table function to the dynamo class object/module?
        dynamodb_client = sts.assume_role(ssm_params.SRA_SECURITY_ACCT, sts.CONFIGURATION_ROLE, "dynamodb", sts.HOME_REGION)

        if dynamodb.table_exists(STATE_TABLE, dynamodb_client) is False:
            dynamodb.create_table(STATE_TABLE, dynamodb_client)
        dynamodb_resource = sts.assume_role_resource(ssm_params.SRA_SECURITY_ACCT, sts.CONFIGURATION_ROLE, "dynamodb", sts.HOME_REGION)

        item_found, find_result = dynamodb.find_item(
            STATE_TABLE,
            dynamodb_resource,
            SOLUTION_NAME,
            {
                "arn": f"arn:aws:dynamodb:{sts.HOME_REGION}:{ssm_params.SRA_SECURITY_ACCT}:table/{STATE_TABLE}",
            },
        )
        if item_found is False:
            dynamodb_record_id, dynamodb_date_time = dynamodb.insert_item(STATE_TABLE, dynamodb_resource, SOLUTION_NAME)
        else:
            dynamodb_record_id = find_result["record_id"]
        dynamodb.update_item(
            STATE_TABLE,
            dynamodb_resource,
            SOLUTION_NAME,
            dynamodb_record_id,
            {
                "aws_service": "dynamodb",
                "component_state": "implemented",
                "account": ssm_params.SRA_SECURITY_ACCT,
                "description": "sra state table",
                "component_region": sts.HOME_REGION,
                "component_type": "table",
                "component_name": STATE_TABLE,
                "arn": f"arn:aws:dynamodb:{sts.HOME_REGION}:{ssm_params.SRA_SECURITY_ACCT}:table/{STATE_TABLE}",
                "date_time": dynamodb.get_date_time(),
            },
        )
        CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
        CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1
        LIVE_RUN_DATA["StateTableCreate"] = "Created state table"
    else:
        LOGGER.info(f"DRY_RUN: Create the {STATE_TABLE} state table")
        DRY_RUN_DATA["StateTableCreate"] = f"DRY_RUN: Create the {STATE_TABLE} state table"


def deploy_stage_config_rule_lambda_code():
    global DRY_RUN_DATA
    global LIVE_RUN_DATA
    global CFN_RESPONSE_DATA

    if DRY_RUN is False:
        LOGGER.info("Live run: downloading and staging the config rule code...")
        repo.download_code_library(repo.REPO_ZIP_URL)
        CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
        LIVE_RUN_DATA["CodeDownload"] = "Downloaded code library"
        repo.prepare_config_rules_for_staging(repo.STAGING_UPLOAD_FOLDER, repo.STAGING_TEMP_FOLDER, repo.SOLUTIONS_DIR)
        CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
        LIVE_RUN_DATA["CodePrep"] = "Prepared config rule code for staging"
        s3.stage_code_to_s3(repo.STAGING_UPLOAD_FOLDER, s3.STAGING_BUCKET, "/")
        LIVE_RUN_DATA["CodeStaging"] = "Staged config rule code to staging s3 bucket"
        CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
    else:
        LOGGER.info(f"DRY_RUN: Downloading code library from {repo.REPO_ZIP_URL}")
        LOGGER.info(f"DRY_RUN: Preparing config rules for staging in the {repo.STAGING_UPLOAD_FOLDER} folder")
        LOGGER.info(f"DRY_RUN: Staging config rule code to the {s3.STAGING_BUCKET} staging bucket")

def deploy_sns_configuration_topics(context):
    global DRY_RUN_DATA
    global LIVE_RUN_DATA
    global CFN_RESPONSE_DATA

    topic_search = sns.find_sns_topic(f"{SOLUTION_NAME}-configuration")
    if topic_search is None:
        if DRY_RUN is False:
            LOGGER.info(f"Creating {SOLUTION_NAME}-configuration SNS topic")
            topic_arn = sns.create_sns_topic(f"{SOLUTION_NAME}-configuration", SOLUTION_NAME)
            LIVE_RUN_DATA["SNSCreate"] = f"Created {SOLUTION_NAME}-configuration SNS topic"
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1    

            LOGGER.info(f"Creating SNS topic policy permissions for {topic_arn} on {context.function_name} lambda function")
            # TODO(liamschn): search for permissions on lambda before adding the policy
            lambdas.put_permissions(context.function_name, "sns-invoke", "sns.amazonaws.com", "lambda:InvokeFunction", topic_arn)
            LIVE_RUN_DATA["SNSPermissions"] = "Added lambda sns-invoke permissions for SNS topic"
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1

            LOGGER.info(f"Subscribing {context.invoked_function_arn} to {topic_arn}")
            sns.create_sns_subscription(topic_arn, "lambda", context.invoked_function_arn)
            LIVE_RUN_DATA["SNSSubscription"] = f"Subscribed {context.invoked_function_arn} lambda to {SOLUTION_NAME}-configuration SNS topic"
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1

        else:
            LOGGER.info(f"DRY_RUN: Creating {SOLUTION_NAME}-configuration SNS topic")
            DRY_RUN_DATA["SNSCreate"] = f"DRY_RUN: Create {SOLUTION_NAME}-configuration SNS topic"

            LOGGER.info(
                f"DRY_RUN: Creating SNS topic policy permissions for {SOLUTION_NAME}-configuration SNS topic on {context.function_name} lambda function"
            )
            DRY_RUN_DATA["SNSPermissions"] = "DRY_RUN: Add lambda sns-invoke permissions for SNS topic"

            LOGGER.info(f"DRY_RUN: Subscribing {context.invoked_function_arn} to {SOLUTION_NAME}-configuration SNS topic")
            DRY_RUN_DATA["SNSSubscription"] = f"DRY_RUN: Subscribe {context.invoked_function_arn} lambda to {SOLUTION_NAME}-configuration SNS topic"
    else:
        LOGGER.info(f"{SOLUTION_NAME}-configuration SNS topic already exists.")
        topic_arn = topic_search
    # SNS State table record:
    # TODO(liamschn): move dynamodb resource to the dynamo class object/module
    dynamodb_resource = sts.assume_role_resource(ssm_params.SRA_SECURITY_ACCT, sts.CONFIGURATION_ROLE, "dynamodb", sts.HOME_REGION)
    item_found, find_result = dynamodb.find_item(
        STATE_TABLE,
        dynamodb_resource,
        SOLUTION_NAME,
        {
            "arn": topic_arn,
        },
    )
    if item_found is False:
        sns_record_id, sns_date_time = dynamodb.insert_item(STATE_TABLE, dynamodb_resource, SOLUTION_NAME)
    else:
        sns_record_id = find_result["record_id"]

    dynamodb.update_item(
        STATE_TABLE,
        dynamodb_resource,
        SOLUTION_NAME,
        sns_record_id,
        {
            "aws_service": "sns",
            "component_state": "implemented",
            "account": ACCOUNT,
            "description": "configuration topic",
            "component_region": sts.HOME_REGION,
            "component_type": "topic",
            "component_name": f"{SOLUTION_NAME}-configuration",
            "arn": topic_arn,
            "date_time": dynamodb.get_date_time(),
        },
    )


    return topic_arn

def deploy_config_rules(region, accounts, resource_properties):
    global DRY_RUN_DATA
    global LIVE_RUN_DATA
    global CFN_RESPONSE_DATA
    for prop in resource_properties:
        if prop.startswith("SRA-BEDROCK-CHECK-"):
            rule_name: str = prop
            LOGGER.info(f"Create operation: retrieving {rule_name} parameters...")
            rule_deploy, rule_accounts, rule_regions, rule_input_params = get_rule_params(rule_name, resource_properties)
            rule_name = rule_name.lower()
            LOGGER.info(f"Create operation: examining {rule_name} resources...")
            if rule_regions:
                LOGGER.info(f"{rule_name} regions: {rule_regions}")
                if region not in rule_regions:
                    LOGGER.info(f"{rule_name} does not apply to {region}; skipping...")
                    continue

            for acct in accounts:

                if rule_deploy is False:
                    continue
                if rule_accounts:
                    LOGGER.info(f"{rule_name} accounts: {rule_accounts}")
                    if acct not in rule_accounts:
                        LOGGER.info(f"{rule_name} does not apply to {acct}; skipping...")
                        continue
                if DRY_RUN is False:
                    # 3a) Deploy IAM role for custom config rule lambda
                    LOGGER.info(f"Deploying IAM role for custom config rule lambda in {acct}")
                    role_arn = deploy_iam_role(acct, rule_name)
                    LIVE_RUN_DATA[f"{rule_name}_{acct}_IAMRole"] = "Deployed IAM role for custom config rule lambda"

                else:
                    LOGGER.info(f"DRY_RUN: Deploying IAM role for custom config rule lambda in {acct}")
                    DRY_RUN_DATA[f"{rule_name}_{acct}_IAMRole"] = "DRY_RUN: Deploy IAM role for custom config rule lambda"
                # 3b) Deploy lambda for custom config rule
                if DRY_RUN is False:
                    # download rule zip file
                    s3_key = f"{SOLUTION_NAME}/rules/{rule_name}/{rule_name}.zip"
                    local_base_path = '/tmp/sra_staging_upload'
                    local_file_path = os.path.join(local_base_path, f'{SOLUTION_NAME}', 'rules', rule_name, f'{rule_name}.zip')
                    s3.download_s3_file(local_file_path, s3_key, s3.STAGING_BUCKET)
                    LIVE_RUN_DATA[f"{rule_name}_{acct}_{region}_LambdaCode"] = "Downloaded custom config rule lambda code"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1

                    LOGGER.info(f"Deploying lambda for custom config rule in {acct} in {region}")
                    lambda_arn = deploy_lambda_function(acct, rule_name, role_arn, region)
                    LIVE_RUN_DATA[f"{rule_name}_{acct}_{region}_Lambda"] = "Deployed custom config lambda function"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1
                else:
                    LOGGER.info(f"DRY_RUN: Deploying lambda for custom config rule in {acct} in {region}")
                    DRY_RUN_DATA[f"{rule_name}_{acct}_{region}_Lambda"] = "DRY_RUN: Deploy custom config lambda function"

                # 3c) Deploy the config rule (requires config_org [non-CT] or config_mgmt [CT] solution)
                if DRY_RUN is False:
                    config_rule_arn = deploy_config_rule(acct, rule_name, lambda_arn, region, rule_input_params)
                    LIVE_RUN_DATA[f"{rule_name}_{acct}_{region}_Config"] = "Deployed custom config rule"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1
                else:
                    LOGGER.info(f"DRY_RUN: Deploying custom config rule in {acct} in {region}")
                    DRY_RUN_DATA[f"{rule_name}_{acct}_{region}_Config"] = "DRY_RUN: Deploy custom config rule"

def deploy_metric_filters_and_alarms(region, accounts, resource_properties):
    global DRY_RUN_DATA
    global LIVE_RUN_DATA
    global CFN_RESPONSE_DATA
    LOGGER.info(f"CloudWatch Metric Filters: {CLOUDWATCH_METRIC_FILTERS}")
    for filter in CLOUDWATCH_METRIC_FILTERS:
        filter_deploy, filter_accounts, filter_regions, filter_params = get_filter_params(filter, resource_properties)
        LOGGER.info(f"{filter} parameters: {filter_params}")
        if filter_deploy is False:
            LOGGER.info(f"{filter} filter not requested (deploy set to false). Skipping...")
            continue
        if filter_regions:
            LOGGER.info(f"{filter} filter regions: {filter_regions}")
            if region not in filter_regions:
                LOGGER.info(f"{filter} filter not requested for {region}. Skipping...")
                continue
        LOGGER.info(f"Raw filter pattern: {CLOUDWATCH_METRIC_FILTERS[filter]}")
        if "BUCKET_NAME_PLACEHOLDER" in CLOUDWATCH_METRIC_FILTERS[filter]:
            LOGGER.info(f"{filter} filter parameter: 'BUCKET_NAME_PLACEHOLDER' found. Updating with bucket info...")
            filter_pattern = build_s3_metric_filter_pattern(filter_params["bucket_names"], CLOUDWATCH_METRIC_FILTERS[filter])
        elif "INPUT_PATH" in CLOUDWATCH_METRIC_FILTERS[filter]:
            filter_pattern = CLOUDWATCH_METRIC_FILTERS[filter].replace("<INPUT_PATH>", filter_params["input_path"])
        else:
            filter_pattern = CLOUDWATCH_METRIC_FILTERS[filter]
        LOGGER.info(f"{filter} filter pattern: {filter_pattern}")

        for acct in accounts:
            # for region in regions:
            # 4a) Deploy KMS keys
            # 4ai) KMS key for SNS topic used by CloudWatch alarms
            if filter_accounts:
                LOGGER.info(f"filter_accounts: {filter_accounts}")
                if acct not in filter_accounts:
                    LOGGER.info(f"{filter} filter not requested for {acct}. Skipping...")
                    continue
            kms.KMS_CLIENT = sts.assume_role(acct, sts.CONFIGURATION_ROLE, "kms", region)
            search_alarm_kms_key, alarm_key_alias, alarm_key_id = kms.check_alias_exists(kms.KMS_CLIENT, f"alias/{ALARM_SNS_KEY_ALIAS}")
            if search_alarm_kms_key is False:
                LOGGER.info(f"alias/{ALARM_SNS_KEY_ALIAS} not found.")
                # TODO(liamschn): search for key itself (by policy) before creating the key; then separate the alias creation from this section
                if DRY_RUN is False:
                    LOGGER.info("Creating SRA alarm KMS key")
                    LOGGER.info("Customizing key policy...")
                    kms_key_policy = json.loads(json.dumps(KMS_KEY_POLICIES[ALARM_SNS_KEY_ALIAS]))
                    LOGGER.info(f"kms_key_policy: {kms_key_policy}")
                    kms_key_policy["Statement"][0]["Principal"]["AWS"] = KMS_KEY_POLICIES[ALARM_SNS_KEY_ALIAS]["Statement"][0]["Principal"][
                        "AWS"
                    ].replace("ACCOUNT_ID", acct)
                    LOGGER.info(f"Customizing key policy...done: {kms_key_policy}")
                    alarm_key_id = kms.create_kms_key(kms.KMS_CLIENT, json.dumps(kms_key_policy), "Key for CloudWatch Alarm SNS Topic Encryption")
                    LOGGER.info(f"Created SRA alarm KMS key: {alarm_key_id}")
                    LIVE_RUN_DATA["KMSKeyCreate"] = "Created SRA alarm KMS key"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1

                    # 4aii KMS alias for SNS topic used by CloudWatch alarms
                    LOGGER.info("Creating SRA alarm KMS key alias")
                    kms.create_alias(kms.KMS_CLIENT, f"alias/{ALARM_SNS_KEY_ALIAS}", alarm_key_id)
                    LIVE_RUN_DATA["KMSAliasCreate"] = "Created SRA alarm KMS key alias"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1

                else:
                    LOGGER.info("DRY_RUN: Creating SRA alarm KMS key")
                    DRY_RUN_DATA["KMSKeyCreate"] = "DRY_RUN: Create SRA alarm KMS key"
                    LOGGER.info("DRY_RUN: Creating SRA alarm KMS key alias")
                    DRY_RUN_DATA["KMSAliasCreate"] = "DRY_RUN: Create SRA alarm KMS key alias"
            else:
                LOGGER.info(f"Found SRA alarm KMS key: {alarm_key_id}")
            
            if DRY_RUN is False:
                # Add KMS resource records to sra state table
                # TODO(liamschn): move dynamodb resource to the dynamo class object/module
                dynamodb_resource = sts.assume_role_resource(ssm_params.SRA_SECURITY_ACCT, sts.CONFIGURATION_ROLE, "dynamodb", sts.HOME_REGION)

                item_found, find_result = dynamodb.find_item(
                    STATE_TABLE,
                    dynamodb_resource,
                    SOLUTION_NAME,
                    {
                        "arn": f"arn:aws:kms:{region}:{acct}:key/{alarm_key_id}",
                    },
                )
                if item_found is False:
                    kms_key_record_id, iam_date_time = dynamodb.insert_item(STATE_TABLE, dynamodb_resource, SOLUTION_NAME)
                else:
                    kms_key_record_id = find_result["record_id"]

                dynamodb.update_item(
                    STATE_TABLE,
                    dynamodb_resource,
                    SOLUTION_NAME,
                    kms_key_record_id,
                    {
                        "aws_service": "kms",
                        "component_state": "implemented",
                        "account": acct,
                        "description": "secrets kms key",
                        "component_region": region,
                        "component_type": "key",
                        "component_name": alarm_key_id,
                        "key_id": alarm_key_id,
                        "arn": f"arn:aws:kms:{region}:{acct}:key/{alarm_key_id}",
                        "date_time": dynamodb.get_date_time(),
                    },
                )

                item_found, find_result = dynamodb.find_item(
                    STATE_TABLE,
                    dynamodb_resource,
                    SOLUTION_NAME,
                    {
                        "arn": f"arn:aws:kms:{region}:{acct}:{ALARM_SNS_KEY_ALIAS}",
                    },
                )
                if item_found is False:
                    kms_alias_record_id, iam_date_time = dynamodb.insert_item(STATE_TABLE, dynamodb_resource, SOLUTION_NAME)
                else:
                    kms_alias_record_id = find_result["record_id"]

                dynamodb.update_item(
                    STATE_TABLE,
                    dynamodb_resource,
                    SOLUTION_NAME,
                    kms_alias_record_id,
                    {
                        "aws_service": "kms",
                        "component_state": "implemented",
                        "account": acct,
                        "description": "secrets kms alias",
                        "component_region": region,
                        "component_type": "alias",
                        "component_name": ALARM_SNS_KEY_ALIAS,
                        "key_id": alarm_key_id,
                        "arn": f"arn:aws:kms:{region}:{acct}:{ALARM_SNS_KEY_ALIAS}",
                        "date_time": dynamodb.get_date_time(),
                    },
                )


            # 4b) SNS topics for alarms
            sns.SNS_CLIENT = sts.assume_role(acct, sts.CONFIGURATION_ROLE, "sns", region)
            topic_search = sns.find_sns_topic(f"{SOLUTION_NAME}-alarms", region, acct)
            if topic_search is None:
                if DRY_RUN is False:
                    LOGGER.info(f"Creating {SOLUTION_NAME}-alarms SNS topic")
                    SRA_ALARM_TOPIC_ARN = sns.create_sns_topic(f"{SOLUTION_NAME}-alarms", SOLUTION_NAME, kms_key=alarm_key_id)
                    LIVE_RUN_DATA["SNSAlarmTopic"] = f"Created {SOLUTION_NAME}-alarms SNS topic (ARN: {SRA_ALARM_TOPIC_ARN})"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1

                    LOGGER.info(f"Setting access for CloudWatch alarms in {acct} to publish to {SOLUTION_NAME}-alarms SNS topic")
                    # TODO(liamschn): search for policy on SNS topic before adding the policy
                    sns.set_topic_access_for_alarms(SRA_ALARM_TOPIC_ARN, acct)
                    LIVE_RUN_DATA["SNSAlarmPolicy"] = "Added policy for CloudWatch alarms to publish to SNS topic"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1

                    LOGGER.info(f"Subscribing {SRA_ALARM_EMAIL} to {SRA_ALARM_TOPIC_ARN}")
                    sns.create_sns_subscription(SRA_ALARM_TOPIC_ARN, "email", SRA_ALARM_EMAIL)
                    LIVE_RUN_DATA["SNSAlarmSubscription"] = f"Subscribed {SRA_ALARM_EMAIL} lambda to {SOLUTION_NAME}-alarms SNS topic"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1

                else:
                    LOGGER.info(f"DRY_RUN: Create {SOLUTION_NAME}-alarms SNS topic")
                    DRY_RUN_DATA["SNSAlarmCreate"] = f"DRY_RUN: Create {SOLUTION_NAME}-alarms SNS topic"

                    LOGGER.info(
                        f"DRY_RUN: Create SNS topic policy for {SOLUTION_NAME}-alarms SNS topic to alow cloudwatch alarm access from {sts.MANAGEMENT_ACCOUNT} account"
                    )
                    DRY_RUN_DATA[
                        "SNSAlarmPermissions"
                    ] = f"DRY_RUN: Create SNS topic policy for {SOLUTION_NAME}-alarms SNS topic to alow cloudwatch alarm access from {sts.MANAGEMENT_ACCOUNT} account"

                    LOGGER.info(f"DRY_RUN: Subscribe {SRA_ALARM_EMAIL} lambda to {SOLUTION_NAME}-alarms SNS topic")
                    DRY_RUN_DATA["SNSAlarmSubscription"] = f"DRY_RUN: Subscribe {SRA_ALARM_EMAIL} lambda to {SOLUTION_NAME}-alarms SNS topic"
            else:
                LOGGER.info(f"{SOLUTION_NAME}-alarms SNS topic already exists.")
                SRA_ALARM_TOPIC_ARN = topic_search
            if DRY_RUN is False:
                # SNS state table record
                # TODO(liamschn): move dynamodb resource to the dynamo class object/module
                dynamodb_resource = sts.assume_role_resource(ssm_params.SRA_SECURITY_ACCT, sts.CONFIGURATION_ROLE, "dynamodb", sts.HOME_REGION)

                item_found, find_result = dynamodb.find_item(
                    STATE_TABLE,
                    dynamodb_resource,
                    SOLUTION_NAME,
                    {
                        "arn": SRA_ALARM_TOPIC_ARN,
                    },
                )
                if item_found is False:
                    sns_record_id, sns_date_time = dynamodb.insert_item(STATE_TABLE, dynamodb_resource, SOLUTION_NAME)
                else:
                    sns_record_id = find_result["record_id"]

                dynamodb.update_item(
                    STATE_TABLE,
                    dynamodb_resource,
                    SOLUTION_NAME,
                    sns_record_id,
                    {
                        "aws_service": "sns",
                        "component_state": "implemented",
                        "account": acct,
                        "description": "alarms sns topic",
                        "component_region": region,
                        "component_type": "topic",
                        "component_name": f"{SOLUTION_NAME}-alarms",
                        "arn": SRA_ALARM_TOPIC_ARN,
                        "date_time": dynamodb.get_date_time(),
                    },
                )


            # 4c) Cloudwatch metric filters and alarms
            # arn:aws:logs:<region>:<account-id>:metric-filter:<filter-name>
            if DRY_RUN is False:
                if filter_deploy is True:
                    cloudwatch.CWLOGS_CLIENT = sts.assume_role(acct, sts.CONFIGURATION_ROLE, "logs", region)
                    cloudwatch.CLOUDWATCH_CLIENT = sts.assume_role(acct, sts.CONFIGURATION_ROLE, "cloudwatch", region)
                    LOGGER.info(f"Filter deploy parameter is 'true'; deploying {filter} CloudWatch metric filter...")
                    deploy_metric_filter(filter_params["log_group_name"], filter, filter_pattern, f"{filter}-metric", "sra-bedrock", "1")
                    LIVE_RUN_DATA[f"{filter}_CloudWatch"] = "Deployed CloudWatch metric filter"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1
                    LOGGER.info(f"DEBUG: Alarm topic ARN: {SRA_ALARM_TOPIC_ARN}")
                    deploy_metric_alarm(
                        f"{filter}-alarm",
                        f"{filter}-metric alarm",
                        f"{filter}-metric",
                        "sra-bedrock",
                        "Sum",
                        10,
                        1,
                        0,
                        "GreaterThanThreshold",
                        "missing",
                        [SRA_ALARM_TOPIC_ARN],
                    )
                    LIVE_RUN_DATA[f"{filter}_CloudWatch_Alarm"] = "Deployed CloudWatch metric alarm"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1
                else:
                    LOGGER.info(f"Filter deploy parameter is 'false'; skipping {filter} CloudWatch metric filter deployment")
                    LIVE_RUN_DATA[f"{filter}_CloudWatch"] = "Filter deploy parameter is 'false'; Skipped CloudWatch metric filter deployment"
            else:
                if filter_deploy is True:
                    LOGGER.info(f"DRY_RUN: Filter deploy parameter is 'true'; Deploy {filter} CloudWatch metric filter...")
                    DRY_RUN_DATA[f"{filter}_CloudWatch"] = "DRY_RUN: Filter deploy parameter is 'true'; Deploy CloudWatch metric filter"
                    LOGGER.info(f"DRY_RUN: Filter deploy parameter is 'true'; Deploy {filter} CloudWatch metric alarm...")
                    DRY_RUN_DATA[f"{filter}_CloudWatch_Alarm"] = "DRY_RUN: Deploy CloudWatch metric alarm"
                else:
                    LOGGER.info(f"DRY_RUN: Filter deploy parameter is 'false'; Skip {filter} CloudWatch metric filter deployment")
                    DRY_RUN_DATA[f"{filter}_CloudWatch"] = "DRY_RUN: Filter deploy parameter is 'false'; Skip CloudWatch metric filter deployment"

def deploy_central_cloudwatch_observability(event):
    global DRY_RUN_DATA
    global LIVE_RUN_DATA
    global CFN_RESPONSE_DATA

    central_observability_params = json.loads(event["ResourceProperties"]["SRA-BEDROCK-CENTRAL-OBSERVABILITY"])
    # TODO(liamschn): create a parameter to choose to deploy central observability or not: deploy_central_observability = true/false
    # 5a) OAM Sink in security account
    cloudwatch.CWOAM_CLIENT = sts.assume_role(SECURITY_ACCOUNT, sts.CONFIGURATION_ROLE, "oam", sts.HOME_REGION)
    search_oam_sink = cloudwatch.find_oam_sink()
    if search_oam_sink[0] is False:
        if DRY_RUN is False:
            LOGGER.info("CloudWatch observability access manager sink not found, creating...")
            oam_sink_arn = cloudwatch.create_oam_sink(cloudwatch.SINK_NAME)
            LOGGER.info(f"CloudWatch observability access manager sink created: {oam_sink_arn}")
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1
            LIVE_RUN_DATA["OAMSinkCreate"] = "Created CloudWatch observability access manager sink"
        else:
            LOGGER.info("DRY_RUN: CloudWatch observability access manager sink not found, creating...")
            DRY_RUN_DATA["OAMSinkCreate"] = "DRY_RUN: Create CloudWatch observability access manager sink"
    else:
        oam_sink_arn = search_oam_sink[1]
        LOGGER.info(f"CloudWatch observability access manager sink found: {oam_sink_arn}")

    # 5b) OAM Sink policy in security account
    cloudwatch.SINK_POLICY = CLOUDWATCH_OAM_SINK_POLICY["sra-oam-sink-policy"]
    cloudwatch.SINK_POLICY["Statement"][0]["Condition"]["ForAnyValue:StringEquals"]["aws:PrincipalOrgID"] = ORGANIZATION_ID
    if search_oam_sink[0] is False and DRY_RUN is True:
        LOGGER.info("DRY_RUN: CloudWatch observability access manager sink doesn't exist; skip search for sink policy...")
        search_oam_sink_policy = False, {}
    else:
        search_oam_sink_policy = cloudwatch.find_oam_sink_policy(oam_sink_arn)
    if search_oam_sink_policy[0] is False:
        if DRY_RUN is False:
            LOGGER.info("CloudWatch observability access manager sink policy not found, creating...")
            cloudwatch.put_oam_sink_policy(oam_sink_arn, cloudwatch.SINK_POLICY)
            LOGGER.info("CloudWatch observability access manager sink policy created")
            LIVE_RUN_DATA["OAMSinkPolicyCreate"] = "Created CloudWatch observability access manager sink policy"
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1
        else:
            LOGGER.info("DRY_RUN: CloudWatch observability access manager sink policy not found, creating...")
            DRY_RUN_DATA["OAMSinkPolicyCreate"] = "DRY_RUN: Create CloudWatch observability access manager sink policy"
    else:
        check_oam_sink_policy = cloudwatch.compare_oam_sink_policy(search_oam_sink_policy[1], cloudwatch.SINK_POLICY)
        if check_oam_sink_policy is False:
            if DRY_RUN is False:
                LOGGER.info("CloudWatch observability access manager sink policy needs updating...")
                cloudwatch.put_oam_sink_policy(oam_sink_arn, cloudwatch.SINK_POLICY)
                LOGGER.info("CloudWatch observability access manager sink policy updated")
                LIVE_RUN_DATA["OAMSinkPolicyUpdate"] = "Updated CloudWatch observability access manager sink policy"
                CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1
            else:
                LOGGER.info("DRY_RUN: CloudWatch observability access manager sink policy needs updating...")
                DRY_RUN_DATA["OAMSinkPolicyUpdate"] = "DRY_RUN: Update CloudWatch observability access manager sink policy"
        else:
            LOGGER.info("CloudWatch observability access manager sink policy is correct")

    # 5c) OAM CloudWatch-CrossAccountSharingRole IAM role
    # Add management account to the bedrock accounts list
    bedrock_and_mgmt_accounts = copy.deepcopy(central_observability_params["bedrock_accounts"])
    bedrock_and_mgmt_accounts.append(sts.MANAGEMENT_ACCOUNT)
    for bedrock_account in bedrock_and_mgmt_accounts:
        for bedrock_region in central_observability_params["regions"]:
            iam.IAM_CLIENT = sts.assume_role(bedrock_account, sts.CONFIGURATION_ROLE, "iam", iam.get_iam_global_region())
            cloudwatch.CROSS_ACCOUNT_TRUST_POLICY = CLOUDWATCH_OAM_TRUST_POLICY[cloudwatch.CROSS_ACCOUNT_ROLE_NAME]
            cloudwatch.CROSS_ACCOUNT_TRUST_POLICY["Statement"][0]["Principal"]["AWS"] = cloudwatch.CROSS_ACCOUNT_TRUST_POLICY["Statement"][0][
                "Principal"
            ]["AWS"].replace("<SECURITY_ACCOUNT>", SECURITY_ACCOUNT)
            search_iam_role = iam.check_iam_role_exists(cloudwatch.CROSS_ACCOUNT_ROLE_NAME)
            if search_iam_role[0] is False:
                LOGGER.info(
                    f"CloudWatch observability access manager cross-account role not found, creating {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role..."
                )
                if DRY_RUN is False:
                    iam.create_role(cloudwatch.CROSS_ACCOUNT_ROLE_NAME, cloudwatch.CROSS_ACCOUNT_TRUST_POLICY, SOLUTION_NAME)
                    LIVE_RUN_DATA["OAMCrossAccountRoleCreate"] = f"Created {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1
                    LOGGER.info(f"Created {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role")
                else:
                    DRY_RUN_DATA["OAMCrossAccountRoleCreate"] = f"DRY_RUN: Create {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role"
            else:
                LOGGER.info(f"CloudWatch observability access manager cross-account role found: {cloudwatch.CROSS_ACCOUNT_ROLE_NAME}")

            # 5d) Attach managed policies to CloudWatch-CrossAccountSharingRole IAM role
            cross_account_policies = [
                "arn:aws:iam::aws:policy/AWSXrayReadOnlyAccess",
                "arn:aws:iam::aws:policy/CloudWatchAutomaticDashboardsAccess",
                "arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess",
            ]
            for policy_arn in cross_account_policies:
                search_attached_policies = iam.check_iam_policy_attached(cloudwatch.CROSS_ACCOUNT_ROLE_NAME, policy_arn)
                if search_attached_policies is False:
                    LOGGER.info(f"Attaching {policy_arn} policy to {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role...")
                    if DRY_RUN is False:
                        iam.attach_policy(cloudwatch.CROSS_ACCOUNT_ROLE_NAME, policy_arn)
                        LIVE_RUN_DATA[
                            "OAMCrossAccountRolePolicyAttach"
                        ] = f"Attached {policy_arn} policy to {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role"
                        CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                        CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1
                        LOGGER.info(f"Attached {policy_arn} policy to {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role")
                    else:
                        DRY_RUN_DATA[
                            "OAMCrossAccountRolePolicyAttach"
                        ] = f"DRY_RUN: Attach {policy_arn} policy to {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role"

            # 5e) OAM link in bedrock account
            cloudwatch.CWOAM_CLIENT = sts.assume_role(bedrock_account, sts.CONFIGURATION_ROLE, "oam", bedrock_region)
            search_oam_link = cloudwatch.find_oam_link(oam_sink_arn)
            if search_oam_link[0] is False:
                if DRY_RUN is False:
                    LOGGER.info("CloudWatch observability access manager link not found, creating...")
                    cloudwatch.create_oam_link(oam_sink_arn)
                    LIVE_RUN_DATA["OAMLinkCreate"] = "Created CloudWatch observability access manager link"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1
                    LOGGER.info("Created CloudWatch observability access manager link")
                else:
                    LOGGER.info("DRY_RUN: CloudWatch observability access manager link not found, creating...")
                    DRY_RUN_DATA["OAMLinkCreate"] = "DRY_RUN: Create CloudWatch observability access manager link"
            else:
                LOGGER.info("CloudWatch observability access manager link found")

def deploy_cloudwatch_dashboard(event):
    global DRY_RUN_DATA
    global LIVE_RUN_DATA
    global CFN_RESPONSE_DATA

    central_observability_params = json.loads(event["ResourceProperties"]["SRA-BEDROCK-CENTRAL-OBSERVABILITY"])

    cloudwatch_dashboard = build_cloudwatch_dashboard(CLOUDWATCH_DASHBOARD, SOLUTION_NAME, central_observability_params["bedrock_accounts"], central_observability_params["regions"])
    cloudwatch.CLOUDWATCH_CLIENT = sts.assume_role(SECURITY_ACCOUNT, sts.CONFIGURATION_ROLE, "cloudwatch", sts.HOME_REGION)
    # sra-bedrock-filter-prompt-injection-metric template ["sra-bedrock-org"]["widgets"][0]["properties"]["metrics"][2]
    # sra-bedrock-filter-sensitive-info-metric template ["sra-bedrock-org"]["widgets"][0]["properties"]["metrics"][3]

    search_dashboard = cloudwatch.find_dashboard(SOLUTION_NAME)
    if search_dashboard[0] is False:
        if DRY_RUN is False:
            LOGGER.info("CloudWatch observability dashboard not found, creating...")
            cloudwatch.create_dashboard(cloudwatch.SOLUTION_NAME, cloudwatch_dashboard)
            LIVE_RUN_DATA["CloudWatchDashboardCreate"] = "Created CloudWatch observability dashboard"
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1
            LOGGER.info("Created CloudWatch observability dashboard")
        else:
            LOGGER.info("DRY_RUN: CloudWatch observability dashboard not found, creating...")
            DRY_RUN_DATA["CloudWatchDashboardCreate"] = "DRY_RUN: Create CloudWatch observability dashboard"
    else:
        LOGGER.info(f"Cloudwatch dashboard already exists: {search_dashboard[1]}")
        # check_dashboard = cloudwatch.compare_dashboard(search_dashboard[1], cloudwatch_dashboard)
        # if check_dashboard is False:
        #     if DRY_RUN is False:
        #         LOGGER.info("CloudWatch observability dashboard needs updating...")
        #         cloudwatch.create_dashboard(cloudwatch.SOLUTION_NAME, cloudwatch_dashboard)
        #         LIVE_RUN_DATA["OAMDashboardUpdate"] = "Updated CloudWatch observability dashboard"
        #         CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
        #         CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1
        #         LOGGER.info("Updated CloudWatch observability dashboard")
        #     else:
        #         LOGGER.info("DRY_RUN: CloudWatch observability dashboard needs updating...")
        #         DRY_RUN_DATA["OAMDashboardUpdate"] = "DRY_RUN: Update CloudWatch observability dashboard"
        # else:
        #     LOGGER.info("CloudWatch observability dashboard is correct")


def create_event(event, context):
    global DRY_RUN_DATA
    global LIVE_RUN_DATA
    global CFN_RESPONSE_DATA

    global SRA_ALARM_TOPIC_ARN
    DRY_RUN_DATA = {}
    LIVE_RUN_DATA = {}

    event_info = {"Event": event}
    LOGGER.info(event_info)
    # Deploy state table
    deploy_state_table()

    # 1) Stage config rule lambda code (global/home region)
    deploy_stage_config_rule_lambda_code()

    # 2) SNS topics for fanout configuration operations (global/home region)
    # TODO(liamschn): change the code to have the create events call the sns topic (by publishing events for accounts/regions) which calls the lambda for configuration/deployment
    topic_arn = deploy_sns_configuration_topics(context)

    # 3, 4, and 5 handled by SNS
    accounts, regions = get_accounts_and_regions(event["ResourceProperties"])

    # 3) Deploy config rules (regional)
    # deploy_config_rules(event)
    create_sns_messages(accounts, regions, topic_arn, event["ResourceProperties"], "configure")

    # 4) deploy kms cmk, cloudwatch metric filters, and SNS topics for alarms (regional)
    # deploy_metric_filters_and_alarms(event)

    # 5) Central CloudWatch Observability (regional)
    deploy_central_cloudwatch_observability(event)

    # 6) Cloudwatch dashboard in security account (home region, security account)
    # TODO(liamschn): Determine if the dashboard will be created if all the above is done asynchronously
    deploy_cloudwatch_dashboard(event)

    # End
    # TODO(liamschn): Consider the 256 KB limit for any cloudwatch log message
    if DRY_RUN is False:
        LOGGER.info(json.dumps({"RUN STATS": CFN_RESPONSE_DATA, "RUN DATA": LIVE_RUN_DATA}))
    else:
        LOGGER.info(json.dumps({"RUN STATS": CFN_RESPONSE_DATA, "RUN DATA": DRY_RUN_DATA}))

    if RESOURCE_TYPE == iam.CFN_CUSTOM_RESOURCE:
        LOGGER.info("Resource type is a custom resource")
        cfnresponse.send(event, context, cfnresponse.SUCCESS, CFN_RESPONSE_DATA, CFN_RESOURCE_ID)
    else:
        LOGGER.info("Resource type is not a custom resource")
    return CFN_RESOURCE_ID


def update_event(event, context):
    # TODO(liamschn): handle CFN update events; use case: change from DRY_RUN = False to DRY_RUN = True or vice versa
    # TODO(liamschn): handle CFN update events; use case: add additional config rules via new rules in code (i.e. ...\rules\new_rule\app.py)
    # TODO(liamschn): handle CFN update events; use case: changing config rule parameters (i.e. deploy, accounts, regions, input_params)
    # TODO(liamschn): handle CFN update events; use case: setting deploy = false should remove the config rule
    global DRY_RUN_DATA
    LOGGER.info("update event function")
    # Temp calling create_event so that an update will actually do something; need to determine if this is the best way or not.
    create_event(event, context)
    # data = sra_s3.s3_resource_check()
    # TODO(liamschn): update data dictionary
    # data = {"data": "no info"}
    # if RESOURCE_TYPE != "Other":
    #     cfnresponse.send(event, context, cfnresponse.SUCCESS, data, CFN_RESOURCE_ID)
    return CFN_RESOURCE_ID


def delete_event(event, context):
    # TODO(liamschn): handle delete error if IAM policy is updated out-of-band - botocore.errorfactory.DeleteConflictException: An error occurred (DeleteConflict) when calling the DeletePolicy operation: This policy has more than one version. Before you delete a policy, you must delete the policy's versions. The default version is deleted with the policy.
    global DRY_RUN_DATA
    global LIVE_RUN_DATA
    global CFN_RESPONSE_DATA
    DRY_RUN_DATA = {}
    LIVE_RUN_DATA = {}
    LOGGER.info("delete event function")
    # 1) Delete SNS topic
    # 1a) Delete configuration topic
    sns.SNS_CLIENT = sts.assume_role(sts.MANAGEMENT_ACCOUNT, sts.CONFIGURATION_ROLE, "sns", sts.HOME_REGION)
    topic_search = sns.find_sns_topic(f"{SOLUTION_NAME}-configuration")
    if topic_search is not None:
        if DRY_RUN is False:
            LOGGER.info(f"Deleting {SOLUTION_NAME}-configuration SNS topic")
            LIVE_RUN_DATA["SNSDelete"] = f"Deleted {SOLUTION_NAME}-configuration SNS topic"
            sns.delete_sns_topic(topic_search)
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
        else:
            LOGGER.info(f"DRY_RUN: Deleting {SOLUTION_NAME}-configuration SNS topic")
            DRY_RUN_DATA["SNSDelete"] = f"DRY_RUN: Delete {SOLUTION_NAME}-configuration SNS topic"
    else:
        LOGGER.info(f"{SOLUTION_NAME}-configuration SNS topic does not exist.")

    # 2) Delete Central CloudWatch Observability
    central_observability_params = json.loads(event["ResourceProperties"]["SRA-BEDROCK-CENTRAL-OBSERVABILITY"])

    cloudwatch.CWOAM_CLIENT = sts.assume_role(SECURITY_ACCOUNT, sts.CONFIGURATION_ROLE, "oam", sts.HOME_REGION)
    search_oam_sink = cloudwatch.find_oam_sink()
    if search_oam_sink[0] is True:
        oam_sink_arn = search_oam_sink[1]
    else:
        LOGGER.info("Error deleting: CloudWatch observability access manager sink not found; may have to manually delete OAM links")
        oam_sink_arn = "Error:Sink:Arn:Not:Found"

    # Add management account to the bedrock accounts list
    central_observability_params["bedrock_accounts"].append(sts.MANAGEMENT_ACCOUNT)
    for bedrock_account in central_observability_params["bedrock_accounts"]:
        for bedrock_region in central_observability_params["regions"]:
            # 2a) OAM link in bedrock account
            cloudwatch.CWOAM_CLIENT = sts.assume_role(bedrock_account, sts.CONFIGURATION_ROLE, "oam", bedrock_region)
            search_oam_link = cloudwatch.find_oam_link(oam_sink_arn)
            if search_oam_link[0] is True:
                if DRY_RUN is False:
                    LOGGER.info(f"CloudWatch observability access manager link ({search_oam_link[1]}) found, deleting...")
                    cloudwatch.delete_oam_link(search_oam_link[1])
                    LIVE_RUN_DATA["OAMLinkDelete"] = "Deleted CloudWatch observability access manager link"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
                    LOGGER.info("Deleted CloudWatch observability access manager link")
                else:
                    LOGGER.info("DRY_RUN: CloudWatch observability access manager link found, deleting...")
                    DRY_RUN_DATA["OAMLinkDelete"] = "DRY_RUN: Delete CloudWatch observability access manager link"
            else:
                LOGGER.info(f"CloudWatch observability access manager link ({oam_sink_arn}) not found")

            iam.IAM_CLIENT = sts.assume_role(bedrock_account, sts.CONFIGURATION_ROLE, "iam", iam.get_iam_global_region())

            # 2b) Detach managed policies to CloudWatch-CrossAccountSharingRole IAM role
            cross_account_policies = iam.list_attached_iam_policies(cloudwatch.CROSS_ACCOUNT_ROLE_NAME)
            if cross_account_policies is not None:
                if DRY_RUN is False:
                    for policy in cross_account_policies:
                        LOGGER.info(f"Detaching {policy['PolicyArn']} policy from {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role...")
                        iam.detach_policy(cloudwatch.CROSS_ACCOUNT_ROLE_NAME, policy["PolicyArn"])
                        LIVE_RUN_DATA[
                            "OAMCrossAccountRolePolicyDetach"
                        ] = f"Detached {policy['PolicyArn']} policy from {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role"
                        CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                        CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1
                        LOGGER.info(f"Detached {policy['PolicyArn']} policy from {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role")
                else:
                    for policy in cross_account_policies:
                        LOGGER.info(f"DRY_RUN: Detaching {policy['PolicyArn']} policy from {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role...")
                        DRY_RUN_DATA[
                            "OAMCrossAccountRolePolicyDetach"
                        ] = f"DRY_RUN: Detach {policy['PolicyArn']} policy from {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role"
            else:
                LOGGER.info(f"No policies attached to {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role")

            # 2c) Delete CloudWatch-CrossAccountSharingRole IAM role
            search_iam_role = iam.check_iam_role_exists(cloudwatch.CROSS_ACCOUNT_ROLE_NAME)
            if search_iam_role[0] is True:
                if DRY_RUN is False:
                    LOGGER.info(f"Deleting {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role...")
                    iam.delete_role(cloudwatch.CROSS_ACCOUNT_ROLE_NAME)
                    LIVE_RUN_DATA["OAMCrossAccountRoleDelete"] = f"Deleted {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
                    LOGGER.info(f"Deleted {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role")
                else:
                    LOGGER.info(f"DRY_RUN: Deleting {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role...")
                    DRY_RUN_DATA["OAMCrossAccountRoleDelete"] = f"DRY_RUN: Delete {cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role"
            else:
                LOGGER.info(f"{cloudwatch.CROSS_ACCOUNT_ROLE_NAME} IAM role does not exist")

    # 2d) Delete OAM Sink in security account
    cloudwatch.CWOAM_CLIENT = sts.assume_role(SECURITY_ACCOUNT, sts.CONFIGURATION_ROLE, "oam", sts.HOME_REGION)
    if search_oam_sink[0] is True:
        if DRY_RUN is False:
            LOGGER.info("CloudWatch observability access manager sink found, deleting...")
            cloudwatch.delete_oam_sink(oam_sink_arn)
            LIVE_RUN_DATA["OAMSinkDelete"] = "Deleted CloudWatch observability access manager sink"
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
            LOGGER.info("Deleted CloudWatch observability access manager sink")
        else:
            LOGGER.info("DRY_RUN: CloudWatch observability access manager sink found, deleting...")
            DRY_RUN_DATA["OAMSinkDelete"] = "DRY_RUN: Delete CloudWatch observability access manager sink"
    else:
        LOGGER.info("CloudWatch observability access manager sink not found")

    # 3) Delete metric alarms and filters
    for filter in CLOUDWATCH_METRIC_FILTERS:
        filter_deploy, filter_accounts, filter_regions, filter_params = get_filter_params(filter, event)
        for acct in filter_accounts:
            for region in filter_regions:
                # 3a) Delete KMS key (schedule deletion)
                kms.KMS_CLIENT = sts.assume_role(acct, sts.CONFIGURATION_ROLE, "kms", region)
                search_alarm_kms_key, alarm_key_alias, alarm_key_id = kms.check_alias_exists(kms.KMS_CLIENT, f"alias/{ALARM_SNS_KEY_ALIAS}")
                if search_alarm_kms_key is True:
                    if DRY_RUN is False:
                        LOGGER.info(f"Deleting {ALARM_SNS_KEY_ALIAS} KMS key")
                        LIVE_RUN_DATA["KMSDelete"] = f"Deleted {ALARM_SNS_KEY_ALIAS} KMS key"
                        kms.delete_alias(kms.KMS_CLIENT, f"alias/{ALARM_SNS_KEY_ALIAS}")
                        CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                        CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
                        LOGGER.info(f"Deleting {ALARM_SNS_KEY_ALIAS} KMS key ({alarm_key_id})")
                        kms.schedule_key_deletion(kms.KMS_CLIENT, alarm_key_id)
                        CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                        CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
                    else:
                        LOGGER.info(f"DRY_RUN: Deleting {ALARM_SNS_KEY_ALIAS} KMS key")
                        DRY_RUN_DATA["KMSDelete"] = f"DRY_RUN: Delete {ALARM_SNS_KEY_ALIAS} KMS key"
                        LOGGER.info(f"DRY_RUN: Deleting {ALARM_SNS_KEY_ALIAS} KMS key ({alarm_key_id})")
                        DRY_RUN_DATA["KMSDelete"] = f"DRY_RUN: Delete {ALARM_SNS_KEY_ALIAS} KMS key ({alarm_key_id})"
                else:
                    LOGGER.info(f"{ALARM_SNS_KEY_ALIAS} KMS key does not exist.")

                cloudwatch.CWLOGS_CLIENT = sts.assume_role(acct, sts.CONFIGURATION_ROLE, "logs", region)
                cloudwatch.CLOUDWATCH_CLIENT = sts.assume_role(acct, sts.CONFIGURATION_ROLE, "cloudwatch", region)
                if DRY_RUN is False:
                    # 3b) Delete the CloudWatch metric alarm
                    LOGGER.info(f"Deleting {filter}-alarm CloudWatch metric alarm")
                    LIVE_RUN_DATA[f"{filter}-alarm_CloudWatchDelete"] = f"Deleted {filter}-alarm CloudWatch metric alarm"
                    search_metric_alarm = cloudwatch.find_metric_alarm(f"{filter}-alarm")
                    if search_metric_alarm is True:
                        cloudwatch.delete_metric_alarm(f"{filter}-alarm")
                        CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                        CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
                    else:
                        LOGGER.info(f"{filter}-alarm CloudWatch metric alarm does not exist.")

                    # 3c) Delete the CloudWatch metric filter
                    LOGGER.info(f"Deleting {filter} CloudWatch metric filter")
                    LIVE_RUN_DATA[f"{filter}_CloudWatchDelete"] = f"Deleted {filter} CloudWatch metric filter"
                    search_metric_filter = cloudwatch.find_metric_filter(filter_params["log_group_name"], filter)
                    if search_metric_filter is True:
                        cloudwatch.delete_metric_filter(filter_params["log_group_name"], filter)
                        CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                        CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
                    else:
                        LOGGER.info(f"{filter} CloudWatch metric filter does not exist.")

                else:
                    LOGGER.info(f"DRY_RUN: Deleting {filter} CloudWatch metric filter")
                    DRY_RUN_DATA[f"{filter}_CloudWatchDelete"] = f"DRY_RUN: Delete {filter} CloudWatch metric filter"

                # 3d) Delete the alarm topic
                sns.SNS_CLIENT = sts.assume_role(acct, sts.CONFIGURATION_ROLE, "sns", region)
                alarm_topic_search = sns.find_sns_topic(f"{SOLUTION_NAME}-alarms", region, acct)
                if alarm_topic_search is not None:
                    if DRY_RUN is False:
                        LOGGER.info(f"Deleting {SOLUTION_NAME}-alarms SNS topic")
                        LIVE_RUN_DATA["SNSDelete"] = f"Deleted {SOLUTION_NAME}-alarms SNS topic"
                        sns.delete_sns_topic(alarm_topic_search)
                        CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                        CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
                    else:
                        LOGGER.info(f"DRY_RUN: Deleting {SOLUTION_NAME}-alarms SNS topic")
                        DRY_RUN_DATA["SNSDelete"] = f"DRY_RUN: Delete {SOLUTION_NAME}-alarms SNS topic"
                else:
                    LOGGER.info(f"{SOLUTION_NAME}-alarms SNS topic does not exist.")

    # 4) Delete config rules
    # TODO(liamschn): deal with invalid rule names
    # TODO(liamschn): deal with invalid account IDs
    accounts, regions = get_accounts_and_regions(event["ResourceProperties"])
    for prop in event["ResourceProperties"]:
        if prop.startswith("SRA-BEDROCK-CHECK-"):
            rule_name: str = prop
            LOGGER.info(f"Delete operation: retrieving {rule_name} parameters...")
            # rule_deploy, rule_input_params = get_rule_params(rule_name, event["ResourceProperties"])
            rule_name = rule_name.lower()
            LOGGER.info(f"Delete operation: examining {rule_name} resources...")

            for acct in accounts:
                for region in regions:
                    # 4a) Delete the config rule
                    config.CONFIG_CLIENT = sts.assume_role(acct, sts.CONFIGURATION_ROLE, "config", region)
                    config_rule_search = config.find_config_rule(rule_name)
                    if config_rule_search[0] is True:
                        if DRY_RUN is False:
                            LOGGER.info(f"Deleting {rule_name} config rule for account {acct} in {region}")
                            config.delete_config_rule(rule_name)
                            LIVE_RUN_DATA[f"{rule_name}_{acct}_{region}_Delete"] = f"Deleted {rule_name} custom config rule"
                            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                            CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
                        else:
                            LOGGER.info(f"DRY_RUN: Deleting {rule_name} config rule for account {acct} in {region}")
                    else:
                        LOGGER.info(f"{rule_name} config rule for account {acct} in {region} does not exist.")
                        DRY_RUN_DATA[f"{rule_name}_{acct}_{region}_Delete"] = f"DRY_RUN: Delete {rule_name} custom config rule"

                    # 4b) Delete lambda for custom config rule
                    lambdas.LAMBDA_CLIENT = sts.assume_role(acct, sts.CONFIGURATION_ROLE, "lambda", region)
                    lambda_search = lambdas.find_lambda_function(rule_name)
                    if lambda_search is not None:
                        if DRY_RUN is False:
                            LOGGER.info(f"Deleting {rule_name} lambda function for account {acct} in {region}")
                            lambdas.delete_lambda_function(rule_name)
                            LIVE_RUN_DATA[f"{rule_name}_{acct}_{region}_Delete"] = f"Deleted {rule_name} lambda function"
                            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                            CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
                        else:
                            LOGGER.info(f"DRY_RUN: Deleting {rule_name} lambda function for account {acct} in {region}")
                            DRY_RUN_DATA[f"{rule_name}_{acct}_{region}_Delete"] = f"DRY_RUN: Delete {rule_name} lambda function"
                    else:
                        LOGGER.info(f"{rule_name} lambda function for account {acct} in {region} does not exist.")

            # 5) Detach IAM policies
            # TODO(liamschn): handle case where policy is not found attached_policies = None
            iam.IAM_CLIENT = sts.assume_role(acct, sts.CONFIGURATION_ROLE, "iam", REGION)
            attached_policies = iam.list_attached_iam_policies(rule_name)
            if attached_policies is not None:
                if DRY_RUN is False:
                    for policy in attached_policies:
                        LOGGER.info(f"Detaching {policy['PolicyName']} IAM policy from account {acct} in {region}")
                        iam.detach_policy(rule_name, policy["PolicyArn"])
                        LIVE_RUN_DATA[
                            f"{rule_name}_{acct}_{region}_PolicyDetach"
                        ] = f"Detached {policy['PolicyName']} IAM policy from account {acct} in {region}"
                        CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                else:
                    LOGGER.info(f"DRY_RUN: Detach {policy['PolicyName']} IAM policy from account {acct} in {region}")
                    DRY_RUN_DATA[
                        f"{rule_name}_{acct}_{region}_Delete"
                    ] = f"DRY_RUN: Detach {policy['PolicyName']} IAM policy from account {acct} in {region}"

            # 6) Delete IAM policy
            policy_arn = f"arn:{sts.PARTITION}:iam::{acct}:policy/{rule_name}-lamdba-basic-execution"
            LOGGER.info(f"Policy ARN: {policy_arn}")
            policy_search = iam.check_iam_policy_exists(policy_arn)
            if policy_search is True:
                if DRY_RUN is False:
                    LOGGER.info(f"Deleting {rule_name}-lamdba-basic-execution IAM policy for account {acct} in {region}")
                    iam.delete_policy(policy_arn)
                    LIVE_RUN_DATA[f"{rule_name}_{acct}_{region}_Delete"] = f"Deleted {rule_name} IAM policy"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
                else:
                    LOGGER.info(f"DRY_RUN: Delete {rule_name}-lamdba-basic-execution IAM policy for account {acct} in {region}")
                    DRY_RUN_DATA[
                        f"{rule_name}_{acct}_{region}_PolicyDelete"
                    ] = f"DRY_RUN: Delete {rule_name}-lamdba-basic-execution IAM policy for account {acct} in {region}"

            policy_arn2 = f"arn:{sts.PARTITION}:iam::{acct}:policy/{rule_name}"
            LOGGER.info(f"Policy ARN: {policy_arn2}")
            policy_search = iam.check_iam_policy_exists(policy_arn2)
            if policy_search is True:
                if DRY_RUN is False:
                    LOGGER.info(f"Deleting {rule_name} IAM policy for account {acct} in {region}")
                    iam.delete_policy(policy_arn2)
                    LIVE_RUN_DATA[f"{rule_name}_{acct}_{region}_Delete"] = f"Deleted {rule_name} IAM policy"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
                else:
                    LOGGER.info(f"DRY_RUN: Delete {rule_name} IAM policy for account {acct} in {region}")
                    DRY_RUN_DATA[
                        f"{rule_name}_{acct}_{region}_PolicyDelete"
                    ] = f"DRY_RUN: Delete {rule_name} IAM policy for account {acct} in {region}"

            # 7) Delete IAM execution role for custom config rule lambda
            role_search = iam.check_iam_role_exists(rule_name)
            if role_search[0] is True:
                if DRY_RUN is False:
                    LOGGER.info(f"Deleting {rule_name} IAM role for account {acct} in {region}")
                    iam.delete_role(rule_name)
                    LIVE_RUN_DATA[f"{rule_name}_{acct}_{region}_Delete"] = f"Deleted {rule_name} IAM role"
                    CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
                    CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] -= 1
                else:
                    LOGGER.info(f"DRY_RUN: Delete {rule_name} IAM role for account {acct} in {region}")
                    DRY_RUN_DATA[f"{rule_name}_{acct}_{region}_RoleDelete"] = f"DRY_RUN: Delete {rule_name} IAM role for account {acct} in {region}"
            else:
                LOGGER.info(f"{rule_name} IAM role for account {acct} in {region} does not exist.")
    # TODO(liamschn): Consider the 256 KB limit for any cloudwatch log message
    if DRY_RUN is False:
        LOGGER.info(json.dumps({"RUN STATS": CFN_RESPONSE_DATA, "RUN DATA": LIVE_RUN_DATA}))
    else:
        LOGGER.info(json.dumps({"RUN STATS": CFN_RESPONSE_DATA, "RUN DATA": DRY_RUN_DATA}))

    if RESOURCE_TYPE != "Other":
        cfnresponse.send(event, context, cfnresponse.SUCCESS, CFN_RESPONSE_DATA, CFN_RESOURCE_ID)


def create_sns_messages(accounts: list, regions: list, sns_topic_arn: str, resource_properties: dict, action: str, ) -> None:
    """Create SNS Message.

    Args:
        accounts: Account List
        regions: list of AWS regions
        sns_topic_arn: SNS Topic ARN
        action: Action
    """
    LOGGER.info("Creating SNS Messages...")
    sns_messages = []
    LOGGER.info("ResourceProperties found in event")

    for region in regions:
            sns_message = {"Accounts": accounts, "Region": region, "ResourceProperties": resource_properties, "Action": action}
            sns_messages.append(
                {
                    "Id": region,
                    "Message": json.dumps(sns_message),
                    "Subject": "SRA Bedrock Configuration",
                }
            )
    sns.process_sns_message_batches(sns_messages, sns_topic_arn)


def process_sns_records(event) -> None:
    """Process SNS records.

    Args:
        records: list of SNS event records
    """
    LOGGER.info("Processing SNS records...")
    for record in event["Records"]:
        record["Sns"]["Message"] = json.loads(record["Sns"]["Message"])
        LOGGER.info({"SNS Record": record})
        message = record["Sns"]["Message"]
        if message["Action"] == "configure":
            LOGGER.info("Continuing process to enable SRA security controls for Bedrock (sns event)")

            # 3) Deploy config rules (regional)
            message['Accounts'].append(sts.MANAGEMENT_ACCOUNT)
            deploy_config_rules(
                message["Region"],
                message["Accounts"], 
                message["ResourceProperties"],
            )

            # 4) deploy kms cmk, cloudwatch metric filters, and SNS topics for alarms (regional)
            deploy_metric_filters_and_alarms(
                message["Region"],
                message["Accounts"],
                message["ResourceProperties"],
            )

            # # 5) Central CloudWatch Observability (regional)
            # deploy_central_cloudwatch_observability(event)
        else:
            LOGGER.info(f"Action specified is {message['Action']}")

def deploy_iam_role(account_id: str, rule_name: str) -> str:
    """Deploy IAM role.

    Args:
        account_id: AWS account ID
        rule_name: config rule name
    """
    global CFN_RESPONSE_DATA
    iam.IAM_CLIENT = sts.assume_role(account_id, sts.CONFIGURATION_ROLE, "iam", REGION)
    LOGGER.info(f"Deploying IAM {rule_name} execution role for rule lambda in {account_id}...")
    iam_role_search = iam.check_iam_role_exists(rule_name)
    if iam_role_search[0] is False:
        if DRY_RUN is False:
            LOGGER.info(f"Creating {rule_name} IAM role")
            role_arn = iam.create_role(rule_name, iam.SRA_TRUST_DOCUMENTS["sra-config-rule"], SOLUTION_NAME)["Role"]["Arn"]
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1

        else:
            LOGGER.info(f"DRY_RUN: Creating {rule_name} IAM role")
    else:
        LOGGER.info(f"{rule_name} IAM role already exists.")
        role_arn = iam_role_search[1]

    # IAM role state table record
    # TODO(liamschn): move dynamodb resource to the dynamo class object/module
    dynamodb_resource = sts.assume_role_resource(ssm_params.SRA_SECURITY_ACCT, sts.CONFIGURATION_ROLE, "dynamodb", sts.HOME_REGION)

    item_found, find_result = dynamodb.find_item(
        STATE_TABLE,
        dynamodb_resource,
        SOLUTION_NAME,
        {
            "arn": role_arn,
        },
    )
    if item_found is False:
        role_record_id, role_date_time = dynamodb.insert_item(STATE_TABLE, dynamodb_resource, SOLUTION_NAME)
    else:
        role_record_id = find_result["record_id"]

    dynamodb.update_item(
        STATE_TABLE,
        dynamodb_resource,
        SOLUTION_NAME,
        role_record_id,
        {
            "aws_service": "iam",
            "component_state": "implemented",
            "account": account_id,
            "description": "role for config rule",
            "component_region": "Global",
            "component_type": "role",
            "component_name": rule_name,
            "arn": role_arn,
            "date_time": dynamodb.get_date_time(),
        },
    )

    iam.SRA_POLICY_DOCUMENTS["sra-lambda-basic-execution"]["Statement"][0]["Resource"] = iam.SRA_POLICY_DOCUMENTS["sra-lambda-basic-execution"][
        "Statement"
    ][0]["Resource"].replace("ACCOUNT_ID", account_id)
    iam.SRA_POLICY_DOCUMENTS["sra-lambda-basic-execution"]["Statement"][1]["Resource"] = iam.SRA_POLICY_DOCUMENTS["sra-lambda-basic-execution"][
        "Statement"
    ][1]["Resource"].replace("ACCOUNT_ID", account_id)
    iam.SRA_POLICY_DOCUMENTS["sra-lambda-basic-execution"]["Statement"][1]["Resource"] = iam.SRA_POLICY_DOCUMENTS["sra-lambda-basic-execution"][
        "Statement"
    ][1]["Resource"].replace("CONFIG_RULE_NAME", rule_name)
    LOGGER.info(f"Policy document: {iam.SRA_POLICY_DOCUMENTS['sra-lambda-basic-execution']}")
    policy_arn = f"arn:{sts.PARTITION}:iam::{account_id}:policy/{rule_name}-lamdba-basic-execution"
    iam_policy_search = iam.check_iam_policy_exists(policy_arn)
    if iam_policy_search is False:
        if DRY_RUN is False:
            LOGGER.info(f"Creating {rule_name}-lamdba-basic-execution IAM policy in {account_id}...")
            iam.create_policy(f"{rule_name}-lamdba-basic-execution", iam.SRA_POLICY_DOCUMENTS["sra-lambda-basic-execution"], SOLUTION_NAME)
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1
        else:
            LOGGER.info(f"DRY _RUN: Creating {rule_name}-lamdba-basic-execution IAM policy in {account_id}...")
    else:
        LOGGER.info(f"{rule_name}-lamdba-basic-execution IAM policy already exists")

    # IAM policy state table record
    # TODO(liamschn): move dynamodb resource to the dynamo class object/module
    dynamodb_resource = sts.assume_role_resource(ssm_params.SRA_SECURITY_ACCT, sts.CONFIGURATION_ROLE, "dynamodb", sts.HOME_REGION)

    item_found, find_result = dynamodb.find_item(
        STATE_TABLE,
        dynamodb_resource,
        SOLUTION_NAME,
        {
            "arn": policy_arn,
        },
    )
    if item_found is False:
        policy1_record_id, policy1_date_time = dynamodb.insert_item(STATE_TABLE, dynamodb_resource, SOLUTION_NAME)
    else:
        policy1_record_id = find_result["record_id"]

    dynamodb.update_item(
        STATE_TABLE,
        dynamodb_resource,
        SOLUTION_NAME,
        policy1_record_id,
        {
            "aws_service": "iam",
            "component_state": "implemented",
            "account": account_id,
            "description": "policy for config rule role",
            "component_region": "Global",
            "component_type": "policy",
            "component_name": f"{rule_name}-lamdba-basic-execution",
            "arn": policy_arn,
            "date_time": dynamodb.get_date_time(),
        },
    )

    policy_arn2 = f"arn:{sts.PARTITION}:iam::{account_id}:policy/{rule_name}"
    iam_policy_search2 = iam.check_iam_policy_exists(policy_arn2)
    if iam_policy_search2 is False:
        if DRY_RUN is False:
            LOGGER.info(f"Creating {rule_name} IAM policy in {account_id}...")
            iam.create_policy(f"{rule_name}", IAM_POLICY_DOCUMENTS[rule_name], SOLUTION_NAME)
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["resources_deployed"] += 1
        else:
            LOGGER.info(f"DRY _RUN: Creating {rule_name} IAM policy in {account_id}...")
    else:
        LOGGER.info(f"{rule_name} IAM policy already exists")

    # IAM policy state table record
    # TODO(liamschn): move dynamodb resource to the dynamo class object/module
    dynamodb_resource = sts.assume_role_resource(ssm_params.SRA_SECURITY_ACCT, sts.CONFIGURATION_ROLE, "dynamodb", sts.HOME_REGION)

    item_found, find_result = dynamodb.find_item(
        STATE_TABLE,
        dynamodb_resource,
        SOLUTION_NAME,
        {
            "arn": policy_arn2,
        },
    )
    if item_found is False:
        policy2_record_id, policy2_date_time = dynamodb.insert_item(STATE_TABLE, dynamodb_resource, SOLUTION_NAME)
    else:
        policy2_record_id = find_result["record_id"]

    dynamodb.update_item(
        STATE_TABLE,
        dynamodb_resource,
        SOLUTION_NAME,
        policy2_record_id,
        {
            "aws_service": "iam",
            "component_state": "implemented",
            "account": account_id,
            "description": "policy for config rule role",
            "component_region": "Global",
            "component_type": "policy",
            "component_name": f"{rule_name}-lamdba-basic-execution",
            "arn": policy_arn2,
            "date_time": dynamodb.get_date_time(),
        },
    )

    policy_attach_search1 = iam.check_iam_policy_attached(rule_name, policy_arn)
    if policy_attach_search1 is False:
        if DRY_RUN is False:
            LOGGER.info(f"Attaching {rule_name}-lamdba-basic-execution policy to {rule_name} IAM role in {account_id}...")
            iam.attach_policy(rule_name, policy_arn)
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1
        else:
            LOGGER.info(f"DRY_RUN: attaching {rule_name}-lamdba-basic-execution policy to {rule_name} IAM role in {account_id}...")

    policy_attach_search2 = iam.check_iam_policy_attached(rule_name, f"arn:{sts.PARTITION}:iam::aws:policy/service-role/AWSConfigRulesExecutionRole")
    if policy_attach_search2 is False:
        if DRY_RUN is False:
            LOGGER.info(f"Attaching AWSConfigRulesExecutionRole policy to {rule_name} IAM role in {account_id}...")
            iam.attach_policy(rule_name, f"arn:{sts.PARTITION}:iam::aws:policy/service-role/AWSConfigRulesExecutionRole")
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1
        else:
            LOGGER.info(f"DRY_RUN: Attaching AWSConfigRulesExecutionRole policy to {rule_name} IAM role in {account_id}...")

    policy_attach_search3 = iam.check_iam_policy_attached(rule_name, f"arn:{sts.PARTITION}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    if policy_attach_search3 is False:
        if DRY_RUN is False:
            LOGGER.info(f"Attaching AWSConfigRulesExecutionRole policy to {rule_name} IAM role in {account_id}...")
            iam.attach_policy(rule_name, f"arn:{sts.PARTITION}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1
        else:
            LOGGER.info(f"DRY_RUN: Attaching AWSLambdaBasicExecutionRole policy to {rule_name} IAM role in {account_id}...")

    policy_attach_search4 = iam.check_iam_policy_attached(rule_name, policy_arn2)
    if policy_attach_search4 is False:
        if DRY_RUN is False:
            LOGGER.info(f"Attaching {rule_name} to {rule_name} IAM role in {account_id}...")
            iam.attach_policy(rule_name, policy_arn2)
            CFN_RESPONSE_DATA["deployment_info"]["action_count"] += 1
            CFN_RESPONSE_DATA["deployment_info"]["configuration_changes"] += 1
        else:
            LOGGER.info(f"DRY_RUN: attaching {rule_name} to {rule_name} IAM role in {account_id}...")

    return role_arn


def deploy_lambda_function(account_id: str, rule_name: str, role_arn: str, region: str) -> str:
    """Deploy lambda function.

    Args:
        account_id: AWS account ID
        config_rule_name: config rule name
        role_arn: IAM role ARN
        regions: list of regions to deploy the lambda function
    """
    lambdas.LAMBDA_CLIENT = sts.assume_role(account_id, sts.CONFIGURATION_ROLE, "lambda", region)
    LOGGER.info(f"Deploying lambda function for {rule_name} config rule to {account_id} in {region}...")
    lambda_function_search = lambdas.find_lambda_function(rule_name)
    if lambda_function_search == None:
        LOGGER.info(f"{rule_name} lambda function not found in {account_id}.  Creating...")
        lambda_source_zip = f"/tmp/sra_staging_upload/{SOLUTION_NAME}/rules/{rule_name}/{rule_name}.zip"
        LOGGER.info(f"Lambda zip file: {lambda_source_zip}")
        lambda_create = lambdas.create_lambda_function(
            lambda_source_zip,
            role_arn,
            rule_name,
            "app.lambda_handler",
            "python3.12",
            900,
            512,
            SOLUTION_NAME,
        )
        lambda_arn = lambda_create["Configuration"]["FunctionArn"]
    else:
        LOGGER.info(f"{rule_name} already exists in {account_id}.  Search result: {lambda_function_search}")
        lambda_arn = lambda_function_search["Configuration"]["FunctionArn"]

    # Lambda state table record
    # TODO(liamschn): move dynamodb resource to the dynamo class object/module
    dynamodb_resource = sts.assume_role_resource(ssm_params.SRA_SECURITY_ACCT, sts.CONFIGURATION_ROLE, "dynamodb", sts.HOME_REGION)

    item_found, find_result = dynamodb.find_item(
        STATE_TABLE,
        dynamodb_resource,
        SOLUTION_NAME,
        {
            "arn": lambda_arn,
        },
    )
    if item_found is False:
        lambda_record_id, lambda_date_time = dynamodb.insert_item(STATE_TABLE, dynamodb_resource, SOLUTION_NAME)
    else:
        lambda_record_id = find_result["record_id"]

    dynamodb.update_item(
        STATE_TABLE,
        dynamodb_resource,
        SOLUTION_NAME,
        lambda_record_id,
        {
            "aws_service": "lambda",
            "component_state": "implemented",
            "account": account_id,
            "description": "lambda for config rule",
            "component_region": region,
            "component_type": "lambda",
            "component_name": rule_name,
            "arn": lambda_arn,
            "date_time": dynamodb.get_date_time(),
        },
    )


    return lambda_arn


def deploy_config_rule(account_id: str, rule_name: str, lambda_arn: str, region: str, input_params: dict) -> None:
    """Deploy config rule.

    Args:
        account_id: AWS account ID
        rule_name: config rule name
        lambda_arn: lambda function ARN
        regions: list of regions to deploy the config rule
        input_params: input parameters for the config rule
    """
    LOGGER.info(f"Deploying {rule_name} config rule to {account_id} in {region}...")
    config.CONFIG_CLIENT = sts.assume_role(account_id, sts.CONFIGURATION_ROLE, "config", region)
    config_rule_search = config.find_config_rule(rule_name)
    if config_rule_search[0] is False:
        if DRY_RUN is False:
            LOGGER.info(f"Creating Config policy permissions for {rule_name} lambda function in {account_id} in {region}...")
            # TODO(liamschn): search for permissions on lambda before adding the policy
            lambdas.put_permissions_acct(rule_name, "config-invoke", "config.amazonaws.com", "lambda:InvokeFunction", account_id)
            LOGGER.info(f"Creating {rule_name} config rule in {account_id} in {region}...")
            # TODO(liamschn): Determine if we need to add a description for the config rules
            # TODO(liamschn): Determine what we will do for input parameters variable in the config rule create function;need an s3 bucket currently
            config_response = config.create_config_rule(
                rule_name,
                lambda_arn,
                "One_Hour",
                "CUSTOM_LAMBDA",
                rule_name,
                input_params,
                "DETECTIVE",
                SOLUTION_NAME,
            )
            config_rule_search = config.find_config_rule(rule_name)
            config_rule_arn = config_rule_search[1]["ConfigRules"][0]["ConfigRuleArn"]
        else:
            LOGGER.info(f"DRY_RUN: Creating Config policy permissions for {rule_name} lambda function in {account_id} in {region}...")
            LOGGER.info(f"DRY_RUN: Creating {rule_name} config rule in {account_id} in {region}...")
    else:
        LOGGER.info(f"{rule_name} config rule already exists.")
        config_rule_arn = config_rule_search[1]["ConfigRules"][0]["ConfigRuleArn"]

    # Config rule state table record
    # TODO(liamschn): move dynamodb resource to the dynamo class object/module
    dynamodb_resource = sts.assume_role_resource(ssm_params.SRA_SECURITY_ACCT, sts.CONFIGURATION_ROLE, "dynamodb", sts.HOME_REGION)

    item_found, find_result = dynamodb.find_item(
        STATE_TABLE,
        dynamodb_resource,
        SOLUTION_NAME,
        {
            "arn": config_rule_arn,
        },
    )
    if item_found is False:
        config_record_id, config_date_time = dynamodb.insert_item(STATE_TABLE, dynamodb_resource, SOLUTION_NAME)
    else:
        config_record_id = find_result["record_id"]

    dynamodb.update_item(
        STATE_TABLE,
        dynamodb_resource,
        SOLUTION_NAME,
        config_record_id,
        {
            "aws_service": "config",
            "component_state": "implemented",
            "account": account_id,
            "description": "custom config rule",
            "component_region": region,
            "component_type": "rule",
            "component_name": rule_name,
            "arn": config_rule_arn,
            "date_time": dynamodb.get_date_time(),
        },
    )
    


def deploy_metric_filter(log_group_name: str, filter_name: str, filter_pattern: str, metric_name: str, metric_namespace: str, metric_value: str):
    """Deploy metric filter.

    Args:
        log_group_name: log group name
        filter_name: filter name
        filter_pattern: filter pattern
        metric_name: metric name
        metric_namespace: metric namespace
        metric_value: metric value
    """
    search_metric_filter = cloudwatch.find_metric_filter(log_group_name, filter_name)
    if search_metric_filter is False:
        if DRY_RUN is False:
            LOGGER.info(f"Deploying metric filter {filter_name} to {log_group_name}...")
            cloudwatch.create_metric_filter(log_group_name, filter_name, filter_pattern, metric_name, metric_namespace, metric_value)
        else:
            LOGGER.info(f"DRY_RUN: Deploy metric filter {filter_name} to {log_group_name}...")
    else:
        LOGGER.info(f"Metric filter {filter_name} already exists.")


def deploy_metric_alarm(
    alarm_name: str,
    alarm_description: str,
    metric_name: str,
    metric_namespace: str,
    metric_statistic: str,
    metric_period: int,
    metric_evaluation_periods: int,
    metric_threshold: float,
    metric_comparison_operator: str,
    metric_treat_missing_data: str,
    alarm_actions: list,
):
    """Deploy metric alarm.

    Args:
        alarm_name: alarm name
        alarm_description: alarm description
        metric_name: metric name
        metric_namespace: metric namespace
        metric_statistic: metric statistic
        metric_period: metric period
        metric_evaluation_periods: metric evaluation periods
        metric_threshold: metric threshold
        metric_comparison_operator: metric comparison operator
        metric_treat_missing_data: metric treat missing data
        alarm_actions: alarm actions
    """
    search_metric_alarm = cloudwatch.find_metric_alarm(alarm_name)
    if search_metric_alarm is False:
        LOGGER.info(f"Deploying metric alarm {alarm_name}...")
        if DRY_RUN is False:
            cloudwatch.create_metric_alarm(
                alarm_name,
                alarm_description,
                metric_name,
                metric_namespace,
                metric_statistic,
                metric_period,
                metric_threshold,
                metric_comparison_operator,
                metric_evaluation_periods,
                metric_treat_missing_data,
                alarm_actions,
            )
        else:
            LOGGER.info(f"DRY_RUN: Deploying metric alarm {alarm_name}...")
    else:
        LOGGER.info(f"Metric alarm {alarm_name} already exists.")


def lambda_handler(event, context):
    global RESOURCE_TYPE
    global LAMBDA_START
    global LAMBDA_FINISH
    LAMBDA_START = dynamodb.get_date_time()
    LOGGER.info(event)
    LOGGER.info({"boto3 version": boto3.__version__})
    try:
        if "ResourceType" in event:
            RESOURCE_TYPE = event["ResourceType"]
            LOGGER.info(f"ResourceType: {RESOURCE_TYPE}")
        else:
            LOGGER.info("ResourceType not found in event.")
            RESOURCE_TYPE = "Other"
        if "Records" not in event and "RequestType" not in event:
            raise ValueError(
                f"The event did not include Records or RequestType. Review CloudWatch logs '{context.log_group_name}' for details."
            ) from None
        elif "Records" in event and event["Records"][0]["EventSource"] == "aws:sns":
            get_resource_parameters(json.loads(event["Records"][0]["Sns"]["Message"]))
            process_sns_records(event)
        elif "RequestType" in event:
            get_resource_parameters(event)
            if event["RequestType"] == "Create":
                LOGGER.info("CREATE EVENT!!")
                create_event(event, context)
            elif event["RequestType"] == "Update":
                LOGGER.info("UPDATE EVENT!!")
                update_event(event, context)
            if event["RequestType"] == "Delete":
                LOGGER.info("DELETE EVENT!!")
                delete_event(event, context)

    except Exception:
        LOGGER.exception("Unexpected!")
        reason = f"See the details in CloudWatch Log Stream: '{context.log_group_name}'"
        if RESOURCE_TYPE != "Other":
            cfnresponse.send(event, context, cfnresponse.FAILED, {}, CFN_RESOURCE_ID, reason=reason)
        LAMBDA_FINISH = dynamodb.get_date_time()
        return {
            "statusCode": 500,
            "lambda_start": LAMBDA_START,
            "lambda_finish": LAMBDA_FINISH,
            "body": "ERROR",
            "dry_run": DRY_RUN,
            "dry_run_data": DRY_RUN_DATA,
        }
    LAMBDA_FINISH = dynamodb.get_date_time()
    return {
        "statusCode": 200,
        "lambda_start": LAMBDA_START,
        "lambda_finish": LAMBDA_FINISH,
        "body": "SUCCESS",
        "dry_run": DRY_RUN,
        "dry_run_data": DRY_RUN_DATA,
    }
