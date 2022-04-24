from typing import Any
from github.File import File
import dateutil
import glob
import os
import pandas as pd
import pathlib
import pykwalify.core
import re
import yaml
import logging
import zoltpy
from zoltpy.connection import ZoltarConnection
from collections import defaultdict

from forecast_validation import PullRequestFileType
from forecast_validation.validation import ValidationStepResult

SCHEMA_FILE = 'forecast_validation/static/schema.yml'
DESIGNATED_MODEL_CACHE_KEY = 'designated_model_cache'

logger = logging.getLogger("hub-validations")

def get_all_metadata_filepaths(
    store: dict[str, Any]
) -> ValidationStepResult:
    directory: pathlib.Path = store["PULL_REQUEST_DIRECTORY_ROOT"]
    metadata_files: list[File] = store["filtered_files"].get(
        PullRequestFileType.METADATA, []
    )
    return ValidationStepResult(
        success=True,
        to_store={"metadata_files": 
            {directory/pathlib.Path(f.filename) for f in metadata_files}
        }
    )

def validate_metadata_contents(metadata, filepath, cache, store: dict[str, Any],):

    # Initialize output
    is_metadata_error = False
    metadata_error_output = []

    core = pykwalify.core.Core(
        source_file=filepath, schema_files=[SCHEMA_FILE]
    )
    core.validate(raise_exception=False, silent=True)

    if len(core.validation_errors) > 0:
        metadata_error_output.extend(['METADATA_ERROR: %s' % err for err in core.validation_errors])
        is_metadata_error = True

    pat_model = re.compile(r"metadata-(.+)\.txt")
    model_name_file = re.findall(pat_model, os.path.basename(filepath))[0]
   

    # This is a critical error and hence do not run further checks.
    if 'model_abbr' not in metadata:
        metadata_error_output.extend(['METADATA_ERROR: model_abbr key not present in the metadata file'])
        is_metadata_error = True
        return is_metadata_error, metadata_error_output

    if model_name_file != metadata['model_abbr']:
        metadata_error_output.append(f"METADATA_ERROR: Model abreviation in metadata inconsistent with folder name for model_abbr={metadata['model_abbr']} as specified in metadata. NOTE: model name on file is: {model_name_file}")
        is_metadata_error = True
    metadata['team_abbr'] = metadata['model_abbr'].split('-')[0]

    # Check if every team has only one `team_model_designation` as `primary`
    if metadata['team_model_designation'] == 'primary' and store["HUB_REPOSITORY_NAME"] == "reichlab/covid19-forecast-hub":
        conn = zoltpy.connection.ZoltarConnection()
        conn.authenticate(os.environ.get('Z_USERNAME'), os.environ.get('Z_PASSWORD'))
        team_models = defaultdict(list)
        project = [project for project in conn.projects if project.name == 'COVID-19 Forecasts'][0]  # http://127.0.0.1:8000/project/44
        for model in project.models:
            if model.team_name in team_models:
                team_models[model.team_name].append(model.name)

        #-team_names = set(model.team_name for model in project.models)
        #print(team_names)
        if team_models[metadata['team_name']]:
            print( team_models[metadata['team_name']],  metadata['model_name'])
            if metadata['model_name'] not in team_models[metadata['team_name']]:
                metadata_error_output.append('METADATA ERROR: %s has more than 1 model designated as \"primary\"' % (metadata['team_abbr']))
                is_metadata_error = True

    if 'team_abbr' in metadata.keys() and 'team_model_designation' in metadata.keys():
        # add designated primary model acche entry to the cache if not present
        if DESIGNATED_MODEL_CACHE_KEY not in cache:
            cache[DESIGNATED_MODEL_CACHE_KEY] = []
        
        # if the current models designation is primary AND the team_name is already present in the cache, then report error
        if metadata['team_abbr'] in cache[DESIGNATED_MODEL_CACHE_KEY] and metadata['team_model_designation'] == 'primary':
            is_metadata_error = True
            metadata_error_output.append('METADATA ERROR: %s has more than 1 model designated as \"primary\"' % (metadata['team_abbr']))
        # else if the current model designation is "primary", then add it to the cache
        elif metadata['team_model_designation'] == 'primary':
            cache[DESIGNATED_MODEL_CACHE_KEY].append(metadata['team_abbr'])
    
    # if `this_model_is_an_emnsemble` is rpesent, show a warning.
    
    # Check for Required Fields
    #required_fields = ['team_name', 'team_abbr', 'model_name', 'model_contributors', 'model_abbr', 'website_url', \
    #                     'license', 'team_model_designation', 'methods', 'ensemble_of_hub_models']
    
    # for field in required_fields:
    #     if field not in metadata.keys():
    #         is_metadata_error = True
    #         metadata_error_output += ["METADATA ERROR: %s missing '%s'" % (filepath, field)]

    # Check methods character length (warning not error)
    # if 'methods' in metadata.keys():
    #     methods_char_lenth = len(metadata['methods'])
    #     if methods_char_lenth > 200:
    #         metadata_error_output += [
    #             "METADATA WARNING: %s methods is too many characters (%i should be less than 200)" %
    #             (filepath, methods_char_lenth)]

    # Check if forecast_startdate is date
    if 'forecast_startdate' in metadata.keys():
        forecast_startdate = str(metadata['forecast_startdate'])
        try:
            dateutil.parser.parse(forecast_startdate)
            is_date = True
        except ValueError:
            is_date = False
        if not is_date:
            is_metadata_error = True
            metadata_error_output += [
                "METADATA ERROR: %s forecast_startdate %s must be a date and should be in YYYY-MM-DD format" %
                (filepath, forecast_startdate)]

    # Check if this_model_is_an_ensemble and this_model_is_unconditional are boolean
    boolean_fields = ['this_model_is_an_ensemble', 'this_model_is_unconditional',
                        'include_in_ensemble_and_visualization', 'ensemble_of_hub_models']
    possible_booleans = ['true', 'false']
    for field in boolean_fields:
        if field in metadata.keys():
            if metadata[field] not in possible_booleans:
                is_metadata_error = True
                metadata_error_output += [
                    "METADATA ERROR: %s '%s' field must be lowercase boolean (true, false) not '%s'" %
                    (filepath, field, metadata[field])]

    # Validate team URLS
    regex = re.compile(
        r'^(?:http|ftp)s?://'    # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'
        r'localhost|'    # localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'    # ...or ip
        r'(?::\d+)?'    # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)

    # if 'team_url' in metadata.keys():
    #     if re.match(regex, str(metadata['team_url'])) is None:
    #         is_metadata_error = True
    #         metadata_error_output += [
    #             "METADATA ERROR: %s 'team_url' field must be a full URL (https://www.example.com) '%s'" %
    #             (filepath, metadata[field])]

    # Validate licenses
    license_df = pd.read_csv('forecast_validation/static/accepted-licenses.csv')
    accepted_licenses = list(license_df['license'])
    if 'license' in metadata.keys():
        if metadata['license'] not in accepted_licenses:
            is_metadata_error = True
            metadata_error_output += [
                "METADATA ERROR: %s 'license' field must be in `accepted-licenses.csv` 'license' column '%s'" %
                (filepath, metadata['license'])]
    return is_metadata_error, metadata_error_output

def validate_metadata_files(
    store: dict[str, Any],
) -> ValidationStepResult:
    success: bool = True
    comments: list[str] = []
    errors: dict[os.PathLike, list[str]] = {}
    correctly_formatted_files: set[os.PathLike] = set()

    logger.info("Checking metadata content...")
    is_metadata_error, metadata_error_output = False, "no errors"
    for file in store["metadata_files"]:
        logger.info("  Checking metadata content for %s", file)
        is_metadata_error, metadata_error_output = check_metadata_file(store = store, filepath = file)
        if is_metadata_error == False:
            logger.info("    %s content validated", file)
            comments.append(
                f"✔️ {file} passed (non-filename) content checks."
            )
            correctly_formatted_files.add(file)
        else:
            file_result = [
                f"Error when validating metadata content: " + e
                for e in metadata_error_output
            ]
            success = False
            error_list = errors.get(file, [])
            error_list.extend(file_result)
            errors[file] = error_list
            for error in file_result:
                logger.error("    " + error)

    return ValidationStepResult(
        success=success,
        comments=comments,
        file_errors=errors
    )

  
def check_metadata_file(store: dict[str, Any], filepath, cache={}):
    with open(filepath, 'rt', encoding='utf8') as stream:
        try:
            Loader = yaml.BaseLoader    # Define Loader to avoid true/false auto conversion
            metadata = yaml.load(stream, Loader=yaml.BaseLoader)
            is_metadata_error, metadata_error_output = validate_metadata_contents(metadata, filepath.as_posix(), cache, store)
            if is_metadata_error:
                return True, metadata_error_output
            else:
                return False, "no errors"
        except yaml.YAMLError as exc:
            return True, [
                "METADATA ERROR: Metadata YAML Format Error for %s file. \
                    \nCommon fixes (if parse error message is unclear):\
                    \n* Try converting all tabs to spaces \
                    \n* Try copying the example metadata file and follow formatting closely \
                    \n Parse Error Message:\n%s \n"
                % (filepath, exc)]


# Check for metadata file
def check_for_metadata(store: dict[str, Any],filepath, cache= {}):
    meta_error_outputs = {}
    is_metadata_error = False
    txt_files = []
    for metadata_file in glob.iglob(filepath + "*.txt", recursive=False):
        txt_files += [os.path.basename(metadata_file)]
    is_metadata_error, metadata_error_output = False, "no errors"
    for metadata_filename in txt_files:
        metadata_filepath = filepath + metadata_filename
        is_metadata_error, metadata_error_output = check_metadata_file(store= store, filepath = metadata_filepath, cache=cache)
        if is_metadata_error:
            meta_error_outputs[metadata_filepath] = metadata_error_output

    return is_metadata_error, meta_error_outputs


def get_metadata_model(filepath):
    team_model = os.path.basename(os.path.dirname(filepath))
    metadata_filename = "metadata-" + team_model + ".txt"
    metdata_dir = filepath + metadata_filename
    model_name = None
    model_abbr = None
    with open(metdata_dir, 'r') as stream:
        try:
            metadata = yaml.safe_load(stream)
            # Output model name and model abbr if exists
            if 'model_name' in metadata.keys():
                model_name = metadata['model_name']
            if 'model_abbr' in metadata.keys():
                model_abbr = metadata['model_abbr']

            return model_name, model_abbr
        except yaml.YAMLError as exc:
            return None, None


def output_duplicate_models(existing_metadata_name, output_errors):
    for mname, mfiledir in existing_metadata_name.items():
        if len(mfiledir) > 1:
            error_string = ["METADATA ERROR: Found duplicate model abbreviation %s - in %s metadata" %
                            (mname, mfiledir)]
            output_errors[mname + "METADATA model_name"] = error_string
    return output_errors
