import datetime
import logging

import html2text
from cvss import CVSS3
from defusedxml import ElementTree as etree
from django.conf import settings

from dojo.models import Endpoint, Finding
from dojo.tools.qualys import csv_parser

logger = logging.getLogger(__name__)


CUSTOM_HEADERS = {
    "CVSS_score": "CVSS Score",
    "ip_address": "IP Address",
    "fqdn": "FQDN",
    "os": "OS",
    "port_status": "Port",
    "vuln_name": "Vulnerability",
    "vuln_description": "Description",
    "solution": "Solution",
    "links": "Links",
    "cve": "CVE",
    "vuln_severity": "Severity",
    "QID": "QID",
    "first_found": "First Found",
    "last_found": "Last Found",
    "found_times": "Found Times",
    "category": "Category",
}

REPORT_HEADERS = [
    "CVSS_score",
    "ip_address",
    "fqdn",
    "os",
    "port_status",
    "vuln_name",
    "vuln_description",
    "solution",
    "links",
    "cve",
    "Severity",
    "QID",
    "first_found",
    "last_found",
    "found_times",
    "category",
]

TYPE_MAP = {
    "Ig": "INFORMATION GATHERED",
    "Practice": "POTENTIAL",
    "Vuln": "CONFIRMED",
}

# Severity mapping taken from
# https://qualysguard.qg2.apps.qualys.com/portal-help/en/malware/knowledgebase/severity_levels.htm
LEGACY_SEVERITY_LOOKUP = {
    1: "Informational",
    2: "Low",
    3: "Medium",
    4: "High",
    5: "Critical",
}
NON_LEGACY_SEVERITY_LOOKUP = {
    "Informational": "Low",
    "Low": "Low",
    "Medium": "Medium",
    "High": "High",
    "Critical": "High",
}


def get_severity(severity_value_str: str | None) -> str:
    severity_value: int = int(severity_value_str or -1)

    sev: str = LEGACY_SEVERITY_LOOKUP.get(severity_value, "Unknown")

    # Non legacy severity is a subset of legacy severity, retrieve it from lookup
    if not settings.USE_QUALYS_LEGACY_SEVERITY_PARSING:
        sev: str = NON_LEGACY_SEVERITY_LOOKUP.get(sev, "Unknown")

    # If we still don't have a severity, default to Informational
    if sev == "Unknown":
        logger.warning(
            "Could not determine severity from severity_value_str: %s",
            severity_value_str,
        )
        sev = "Informational"

    return sev


def htmltext(blob):
    h = html2text.HTML2Text()
    h.ignore_links = False
    return h.handle(blob)


def split_cvss(value, _temp):
    # Check if CVSS field contains the CVSS vector
    if value is None or len(value) == 0 or value == "-":
        return
    if len(value) > 4:
        split = value.split(" (")
        if _temp.get("CVSS_value") is None:
            _temp["CVSS_value"] = float(split[0])
            # remove ")" at the end
        if _temp.get("CVSS_vector") is None:
            _temp["CVSS_vector"] = CVSS3(
                "CVSS:3.0/" + split[1][:-1],
            ).clean_vector()
    else:
        if _temp.get("CVSS_value") is None:
            _temp["CVSS_value"] = float(value)


def parse_finding(host, tree):
    ret_rows = []
    issue_row = {}

    # IP ADDRESS
    issue_row["ip_address"] = host.findtext("IP")

    # FQDN
    issue_row["fqdn"] = host.findtext("DNS")

    # Create Endpoint
    ep = Endpoint(host=issue_row["fqdn"]) if issue_row["fqdn"] else Endpoint(host=issue_row["ip_address"])

    # OS NAME
    issue_row["os"] = host.findtext("OPERATING_SYSTEM")

    # Scan details
    for vuln_details in host.iterfind("VULN_INFO_LIST/VULN_INFO"):
        _temp = issue_row.copy()
        # Port
        _gid = vuln_details.find("QID").attrib["id"]
        _port = vuln_details.findtext("PORT")
        _temp["port_status"] = _port

        _category = str(vuln_details.findtext("CATEGORY"))
        _result = str(vuln_details.findtext("RESULT"))
        _first_found = str(vuln_details.findtext("FIRST_FOUND"))
        _last_found = str(vuln_details.findtext("LAST_FOUND"))
        _times_found = str(vuln_details.findtext("TIMES_FOUND"))

        # Get the date based on the first_seen setting
        try:
            if settings.USE_FIRST_SEEN:
                if date := vuln_details.findtext("FIRST_FOUND"):
                    _temp["date"] = datetime.datetime.strptime(date, "%Y-%m-%dT%H:%M:%SZ").date()
            else:
                if date := vuln_details.findtext("LAST_FOUND"):
                    _temp["date"] = datetime.datetime.strptime(date, "%Y-%m-%dT%H:%M:%SZ").date()
        except Exception:
            _temp["date"] = None

        # Vuln_status
        status = vuln_details.findtext("VULN_STATUS")
        if status == "Active" or status == "Re-Opened" or status == "New":
            _temp["active"] = True
            _temp["mitigated"] = False
            _temp["mitigation_date"] = None
        else:
            _temp["active"] = False
            _temp["mitigated"] = True
            last_fixed = vuln_details.findtext("LAST_FIXED")
            if last_fixed is not None:
                _temp["mitigation_date"] = datetime.datetime.strptime(
                    last_fixed, "%Y-%m-%dT%H:%M:%SZ",
                )
            else:
                _temp["mitigation_date"] = None
        # read cvss value if present
        cvss3 = vuln_details.findtext("CVSS3_FINAL")
        if cvss3 is not None and cvss3 != "-":
            split_cvss(cvss3, _temp)
        else:
            cvss2 = vuln_details.findtext("CVSS_FINAL")
            if cvss2 is not None and cvss2 != "-":
                split_cvss(cvss2, _temp)
                # DefectDojo does not support cvssv2
                _temp["CVSS_vector"] = None

        search = f".//GLOSSARY/VULN_DETAILS_LIST/VULN_DETAILS[@id='{_gid}']"
        vuln_item = tree.find(search)
        if vuln_item is not None:
            finding = Finding()
            # Vuln name
            _temp["vuln_name"] = vuln_item.findtext("TITLE")

            # Vuln Description
            _description = str(vuln_item.findtext("THREAT"))
            # Solution Strips Heading Workaround(s)
            # _temp['solution'] = re.sub('Workaround(s)?:.+\n', '', htmltext(vuln_item.findtext('SOLUTION')))
            _temp["solution"] = htmltext(vuln_item.findtext("SOLUTION"))

            # type
            _type = TYPE_MAP.get(vuln_details.findtext("TYPE"), "Unknown")

            # Vuln_description
            _temp["vuln_description"] = "\n".join(
                [
                    htmltext(_description),
                    htmltext("Type: " + _type),
                    htmltext("Category: " + _category),
                    htmltext("QID: " + str(_gid)),
                    htmltext("Port: " + str(_port)),
                    htmltext("Result Evidence: " + _result),
                    htmltext("First Found: " + _first_found),
                    htmltext("Last Found: " + _last_found),
                    htmltext("Times Found: " + _times_found),
                ],
            )
            # Impact description
            _temp["IMPACT"] = htmltext(vuln_item.findtext("IMPACT"))

            # read cvss value if present and not already read from vuln
            if _temp.get("CVSS_value") is None:
                cvss3 = vuln_item.findtext("CVSS3_SCORE/CVSS3_BASE")
                cvss2 = vuln_item.findtext("CVSS_SCORE/CVSS_BASE")
                if cvss3 is not None and cvss3 != "-":
                    split_cvss(cvss3, _temp)
                else:
                    cvss2 = vuln_item.findtext("CVSS_FINAL")
                    if cvss2 is not None and cvss2 != "-":
                        split_cvss(cvss2, _temp)
                        # DefectDojo does not support cvssv2
                        _temp["CVSS_vector"] = None

            # CVE and LINKS
            _temp_cve_details = vuln_item.iterfind("CVE_ID_LIST/CVE_ID")
            if _temp_cve_details:
                _cl = {
                    cve_detail.findtext("ID"): cve_detail.findtext("URL")
                    for cve_detail in _temp_cve_details
                }
                _temp["cve"] = "\n".join(list(_cl.keys()))
                _temp["links"] = "\n".join(list(_cl.values()))

        # Generate severity from number in XML's 'SEVERITY' field, if not present default to 'Informational'
        sev = get_severity(vuln_item.findtext("SEVERITY"))
        finding = None
        if _temp_cve_details:
            refs = "\n".join(list(_cl.values()))
            finding = Finding(
                title="QID-" + _gid[4:] + " | " + _temp["vuln_name"],
                mitigation=_temp["solution"],
                description=_temp["vuln_description"],
                severity=sev,
                references=refs,
                impact=_temp["IMPACT"],
                date=_temp["date"],
                vuln_id_from_tool=_gid,
            )

        else:
            finding = Finding(
                title="QID-" + _gid[4:] + " | " + _temp["vuln_name"],
                mitigation=_temp["solution"],
                description=_temp["vuln_description"],
                severity=sev,
                references=_gid,
                impact=_temp["IMPACT"],
                date=_temp["date"],
                vuln_id_from_tool=_gid,
            )
        finding.mitigated = _temp["mitigation_date"]
        finding.is_mitigated = _temp["mitigated"]
        finding.active = _temp["active"]
        if _temp.get("CVSS_vector") is not None:
            finding.cvssv3 = _temp.get("CVSS_vector")
        if _temp.get("CVSS_value") is not None:
            finding.cvssv3_score = _temp.get("CVSS_value")
        finding.verified = True
        finding.unsaved_endpoints = []
        finding.unsaved_endpoints.append(ep)
        ret_rows.append(finding)
    return ret_rows


def qualys_parser(qualys_xml_file):
    parser = etree.XMLParser()
    tree = etree.parse(qualys_xml_file, parser)
    host_list = tree.find("HOST_LIST")
    finding_list = []
    if host_list is not None:
        for host in host_list:
            finding_list += parse_finding(host, tree)
    return finding_list


class QualysParser:
    def get_scan_types(self):
        return ["Qualys Scan"]

    def get_label_for_scan_types(self, scan_type):
        return "Qualys Scan"

    def get_description_for_scan_types(self, scan_type):
        return "Qualys WebGUI output files can be imported in XML format."

    def get_findings(self, file, test):
        if file.name.lower().endswith(".csv"):
            return csv_parser.parse_csv(file)
        return qualys_parser(file)
