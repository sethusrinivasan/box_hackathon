"""
test_s3_docs.py — unit tests for S3Docs storage provider.
Verifies contract listing, evidence listing, reading, uploading, and commenting
without requiring real AWS S3 credentials or boto3 installed globally.
"""
import io
import os
import sys
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
for p in (HERE, os.path.join(ROOT, "box")):
    sys.path.insert(0, p)

import local_agent as la


def test_s3_docs_contracts_and_evidence():
    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.side_effect = [
        # contracts prefix
        {"Contents": [{"Key": "contracts/Deal-A.pdf"}, {"Key": "contracts/Deal-B.pdf"}]},
        # evidence prefix
        {"Contents": [{"Key": "evidence/B1-guide.pdf"}, {"Key": "evidence/B2-roadmap.pdf"}]},
    ]
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3

    with patch.dict(sys.modules, {"boto3": mock_boto3}):
        docs = la.S3Docs(bucket="test-reality-check-bucket")
        contracts = docs.contracts()
        assert len(contracts) == 2
        assert contracts[0]["name"] == "Deal-A.pdf"
        assert contracts[1]["id"] == "contracts/Deal-B.pdf"

        evidence = docs.evidence()
        assert len(evidence) == 2
        assert evidence[0]["name"] == "B1-guide.pdf"


def test_s3_docs_read_and_uploads():
    mock_s3 = MagicMock()
    mock_body = MagicMock()
    mock_body.read.return_value = b"%PDF-mock-bytes"
    mock_s3.get_object.return_value = {"Body": mock_body}
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3

    with patch.dict(sys.modules, {"boto3": mock_boto3}):
        docs = la.S3Docs(bucket="test-bucket")
        content = docs.read("contracts/Deal-A.pdf")
        assert content == b"%PDF-mock-bytes"

        # Upload contract
        contract_info = docs.upload_contract("NewDeal.pdf", b"%PDF-new-bytes")
        assert contract_info["id"] == "contracts/NewDeal.pdf"
        mock_s3.put_object.assert_called_with(
            Bucket="test-bucket",
            Key="contracts/NewDeal.pdf",
            Body=b"%PDF-new-bytes",
            ContentType="application/pdf",
        )

        # Upload report
        report_info = docs.upload_report("Report.pdf", b"%PDF-report-bytes")
        assert report_info["id"] == "reports/Report.pdf"


def test_s3_docs_comment():
    mock_s3 = MagicMock()
    # first read raises exception (no prior sidecar)
    mock_s3.get_object.side_effect = Exception("NoSuchKey")
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3

    with patch.dict(sys.modules, {"boto3": mock_boto3}):
        docs = la.S3Docs(bucket="test-bucket")
        key = docs.comment("contracts/Deal-A.pdf", "Flagged risk in Section 3")
        assert key == "reports/Deal-A.comments.txt"
        mock_s3.put_object.assert_called_with(
            Bucket="test-bucket",
            Key="reports/Deal-A.comments.txt",
            Body=b"Flagged risk in Section 3",
            ContentType="text/plain",
        )


def test_get_docs_selects_s3_when_configured():
    # When Box is not configured but REALITY_CHECK_S3_BUCKET is set
    mock_s3 = MagicMock()
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3
    with patch.dict(os.environ, {
        "BOX_CLIENT_ID": "",
        "REALITY_CHECK_S3_BUCKET": "my-aws-native-bucket",
    }):
        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            docs = la.get_docs(offline=False)
            assert docs.kind == "s3"
            assert docs.bucket == "my-aws-native-bucket"


if __name__ == "__main__":
    test_s3_docs_contracts_and_evidence()
    print("PASS test_s3_docs_contracts_and_evidence")
    test_s3_docs_read_and_uploads()
    print("PASS test_s3_docs_read_and_uploads")
    test_s3_docs_comment()
    print("PASS test_s3_docs_comment")
    test_get_docs_selects_s3_when_configured()
    print("PASS test_get_docs_selects_s3_when_configured")
    print("\n4 passed, 0 failed")
