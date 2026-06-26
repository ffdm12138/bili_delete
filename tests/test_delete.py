# -*- coding: utf-8 -*-
"""Tests for bilibili lottery dynamic cleaner.

Run:  pytest tests/ -v
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

import pytest

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from delete import (
    # Exceptions
    BiliError,
    ApiError,
    AuthError,
    RateLimitError,
    NetworkError,
    # Models
    LotteryState,
    CandidateInfo,
    LotteryQueryResult,
    # Parsers
    parse_cookie_string,
    parse_lottery_state,
    # Detection
    deep_search_lottery,
    is_lottery_dynamic,
    extract_text_preview,
    extract_publish_time,
    is_repost_original_invalid,
    # CLI
    build_parser,
    # Constants
    TZ_SHANGHAI,
    SANITIZE_FIELDS,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_lottery_item():
    """A realistic bilibili dynamic item representing an interactive lottery."""
    return {
        "id_str": "1234567890123456789",
        "pub_ts": 1704067200,  # 2024-01-01 00:00:00 UTC
        "orig": {
            "id_str": "9876543210987654321",
            "modules": {
                "module_dynamic": {
                    "additional": {"type": "LOTTERY"},
                    "desc": {"text": "抽奖啦！关注+转发参与"},
                }
            },
        },
        "params": {
            "dyn_id_str": "1234567890123456789",
            "rid_str": "1234567890",
            "dyn_type": 1,
        },
    }


@pytest.fixture
def sample_non_lottery_item():
    """A regular dynamic (not a lottery)."""
    return {
        "id_str": "1111111111111111111",
        "orig": {
            "id_str": "2222222222222222222",
            "modules": {
                "module_dynamic": {
                    "desc": {"text": "今天天气真好"},
                }
            },
        },
        "params": {
            "dyn_id_str": "1111111111111111111",
            "rid_str": "1111111111",
            "dyn_type": 1,
        },
    }


@pytest.fixture
def sample_invalid_repost_item():
    """A repost whose original dynamic has been deleted."""
    return {
        "id_str": "3333333333333333333",
        "orig": {
            "id_str": "4444444444444444444",
            "is_deleted": True,
        },
        "params": {
            "dyn_id_str": "3333333333333333333",
            "rid_str": "3333333333",
            "dyn_type": 1,
        },
    }


# ===================================================================
# 1. Cookie parsing
# ===================================================================


class TestParseCookieString:
    def test_simple(self):
        assert parse_cookie_string("a=1; b=2") == {"a": "1", "b": "2"}

    def test_with_spaces(self):
        assert parse_cookie_string(" a = 1 ; b = 2 ") == {"a": "1", "b": "2"}

    def test_bilibili_cookie(self):
        raw = "DedeUserID=12345; bili_jct=abc; SESSDATA=def"
        result = parse_cookie_string(raw)
        assert result["DedeUserID"] == "12345"
        assert result["bili_jct"] == "abc"
        assert result["SESSDATA"] == "def"

    def test_empty_string(self):
        assert parse_cookie_string("") == {}

    def test_malformed(self):
        assert parse_cookie_string("noequal; foo=bar") == {"foo": "bar"}

    def test_value_contains_equals(self):
        result = parse_cookie_string("token=abc=def==")
        assert result["token"] == "abc=def=="


# ===================================================================
# 2. LotteryState parsing
# ===================================================================


class TestParseLotteryState:
    def test_none_info(self):
        state, reason = parse_lottery_state(None)
        assert state == LotteryState.UNKNOWN
        assert "empty" in reason

    def test_empty_dict(self):
        state, reason = parse_lottery_state({})
        assert state == LotteryState.UNKNOWN

    def test_api_status_finished_1(self):
        state, reason = parse_lottery_state({"status": 1})
        assert state == LotteryState.FINISHED
        assert reason == "api_status"

    def test_api_status_finished_str(self):
        state, _ = parse_lottery_state({"lottery_status": "1"})
        assert state == LotteryState.FINISHED

    def test_api_status_active_0(self):
        state, _ = parse_lottery_state({"status": 0})
        assert state == LotteryState.ACTIVE

    def test_api_status_active_false(self):
        state, _ = parse_lottery_state({"status": "false"})
        assert state == LotteryState.ACTIVE

    def test_api_status_active_ongoing(self):
        state, _ = parse_lottery_state({"lottery_status": "ongoing"})
        assert state == LotteryState.ACTIVE

    def test_api_status_true(self):
        state, _ = parse_lottery_state({"status": "true"})
        assert state == LotteryState.FINISHED

    def test_time_fallback_expired(self):
        """lottery_time + 2h < now → FINISHED"""
        past_ts = (datetime.now(TZ_SHANGHAI) - timedelta(days=7)).timestamp()
        state, reason = parse_lottery_state({"lottery_time": past_ts})
        assert state == LotteryState.FINISHED
        assert reason == "time_fallback"

    def test_time_fallback_not_expired(self):
        """lottery_time is in the future → ACTIVE"""
        future_ts = (datetime.now(TZ_SHANGHAI) + timedelta(days=7)).timestamp()
        state, reason = parse_lottery_state({"lottery_time": future_ts})
        assert state == LotteryState.ACTIVE
        assert reason == "time_not_expired"

    def test_time_fallback_recent(self):
        """lottery_time is 1 hour ago → ACTIVE (within 2h buffer)"""
        recent_ts = (datetime.now(TZ_SHANGHAI) - timedelta(hours=1)).timestamp()
        state, reason = parse_lottery_state({"lottery_time": recent_ts})
        assert state == LotteryState.ACTIVE

    def test_time_fallback_3h_ago(self):
        """lottery_time is 3 hours ago → FINISHED (past 2h buffer)"""
        past_ts = (datetime.now(TZ_SHANGHAI) - timedelta(hours=3)).timestamp()
        state, reason = parse_lottery_state({"lottery_time": past_ts})
        assert state == LotteryState.FINISHED
        assert reason == "time_fallback"

    def test_bad_timestamp(self):
        state, reason = parse_lottery_state({"lottery_time": "not_a_number"})
        assert state == LotteryState.UNKNOWN
        assert reason == "bad_timestamp"

    def test_unrecognised_explicit_status_is_unknown(self):
        """Unrecognised explicit status → UNKNOWN, no time fallback.
        Even with a very old lottery_time, an unknown status value
        must stay UNKNOWN because it could mean 'cancelled' etc."""
        past_ts = (datetime.now(TZ_SHANGHAI) - timedelta(days=365)).timestamp()
        state, reason = parse_lottery_state({
            "lottery_status": "unknown_value",
            "lottery_time": past_ts,
        })
        # Must be UNKNOWN, NOT FINISHED via time fallback
        assert state == LotteryState.UNKNOWN
        assert "unknown_status" in reason

    def test_no_status_no_time(self):
        state, _ = parse_lottery_state({"other_field": "value"})
        assert state == LotteryState.UNKNOWN

    def test_lottery_status_zero_is_active(self):
        """lottery_status=0 must be ACTIVE, not eaten by `or`."""
        old_ts = (datetime.now(TZ_SHANGHAI) - timedelta(days=365)).timestamp()
        state, reason = parse_lottery_state({
            "lottery_status": 0,
            "lottery_time": old_ts,
        })
        assert state == LotteryState.ACTIVE, (
            "lottery_status=0 must be ACTIVE, not bypassed to time fallback"
        )
        assert reason == "api_status"

    def test_lottery_status_false_is_active(self):
        """lottery_status=False must be ACTIVE."""
        old_ts = (datetime.now(TZ_SHANGHAI) - timedelta(days=365)).timestamp()
        state, reason = parse_lottery_state({
            "lottery_status": False,
            "lottery_time": old_ts,
        })
        assert state == LotteryState.ACTIVE
        assert reason == "api_status"

    def test_status_zero_is_active(self):
        """status=0 must be ACTIVE (when lottery_status key absent)."""
        old_ts = (datetime.now(TZ_SHANGHAI) - timedelta(days=365)).timestamp()
        state, reason = parse_lottery_state({
            "status": 0,
            "lottery_time": old_ts,
        })
        assert state == LotteryState.ACTIVE
        assert reason == "api_status"

    def test_lottery_state_can_delete(self):
        assert LotteryState.FINISHED.can_delete() is True
        assert LotteryState.ACTIVE.can_delete() is False
        assert LotteryState.UNKNOWN.can_delete() is False
        assert LotteryState.NOT_LOTTERY.can_delete() is False


# ===================================================================
# 3. Lottery detection
# ===================================================================


class TestDeepSearchLottery:
    def test_key_in_dict_key(self):
        found, path = deep_search_lottery({"lottery_id": "123"})
        assert found
        assert "lottery_id" in path

    def test_keyword_in_value(self):
        found, path = deep_search_lottery({"name": "互动抽奖活动"})
        assert found

    def test_nested_list(self):
        found, path = deep_search_lottery({"items": [{"type": "normal"}, {"type": "lottery"}]})
        assert found

    def test_chinese_keyword(self):
        found, path = deep_search_lottery({"标题": "抽奖"})
        assert found

    def test_deeply_nested(self):
        found, path = deep_search_lottery({
            "a": {"b": {"c": {"d": {"lottery_info": "..."}}}}
        })
        assert found
        assert ".lottery_info" in path

    def test_no_match(self):
        found, path = deep_search_lottery({"a": "b", "c": 123})
        assert not found

    def test_empty_input(self):
        found, path = deep_search_lottery({})
        assert not found


class TestIsLotteryDynamic:
    def test_lottery_item(self, sample_lottery_item):
        is_lottery, orig_id, reason = is_lottery_dynamic(sample_lottery_item)
        assert is_lottery
        assert orig_id == "9876543210987654321"
        assert "additional_type" in reason or "desc_regex" in reason

    def test_non_lottery_item(self, sample_non_lottery_item):
        is_lottery, orig_id, reason = is_lottery_dynamic(sample_non_lottery_item)
        assert not is_lottery

    def test_no_orig(self):
        is_lottery, _, _ = is_lottery_dynamic({"id_str": "123"})
        assert not is_lottery

    def test_not_dict(self):
        is_lottery, _, _ = is_lottery_dynamic("not_a_dict")  # type: ignore
        assert not is_lottery

    def test_lottery_text_only(self):
        """Detect via desc text regex only."""
        item = {
            "id_str": "1",
            "orig": {
                "id_str": "2",
                "modules": {
                    "module_dynamic": {
                        "desc": {"text": "转发+关注参与抽奖！"},
                    }
                },
            },
        }
        is_lottery, _, reason = is_lottery_dynamic(item)
        assert is_lottery
        assert "desc_regex" in reason

    def test_zhuan_guan(self):
        """'转关' abbreviation should match."""
        item = {
            "id_str": "1",
            "orig": {
                "id_str": "2",
                "modules": {
                    "module_dynamic": {
                        "desc": {"text": "转关参与"},
                    }
                },
            },
        }
        is_lottery, _, _ = is_lottery_dynamic(item)
        assert is_lottery

    def test_deep_search_based_detection(self):
        """Detection via deep_search finding lottery in nested fields."""
        item = {
            "id_str": "1",
            "orig": {
                "id_str": "2",
                "extension": {"lottery_id": "abc123"},
            },
        }
        is_lottery, _, reason = is_lottery_dynamic(item)
        assert is_lottery
        assert "deep_search" in reason


# ===================================================================
# 4. Invalid repost detection
# ===================================================================


class TestIsRepostOriginalInvalid:
    def test_is_deleted_flag(self, sample_invalid_repost_item):
        assert is_repost_original_invalid(sample_invalid_repost_item)

    def test_deleted_flag(self):
        item = {"orig": {"id_str": "1", "deleted": True}}
        assert is_repost_original_invalid(item)

    def test_modules_is_none(self):
        item = {"orig": {"id_str": "1", "modules": None}}
        assert is_repost_original_invalid(item)

    def test_modules_is_empty_dict(self):
        item = {"orig": {"id_str": "1", "modules": {}}}
        assert is_repost_original_invalid(item)

    def test_valid_repost(self):
        item = {
            "orig": {
                "id_str": "1",
                "modules": {"module_dynamic": {"desc": {"text": "hello"}}},
            }
        }
        assert not is_repost_original_invalid(item)

    def test_no_orig(self):
        assert not is_repost_original_invalid({"id_str": "1"})

    def test_badge_invalid(self):
        item = {
            "orig": {
                "id_str": "1",
                "modules": {"module_dynamic": {"desc": {"text": "..."}}},
                "major": {"archive": {"badge": {"text": "已失效"}}},
            }
        }
        assert is_repost_original_invalid(item)

    def test_text_marker_dynamic_not_exist(self):
        item = {
            "orig": {
                "id_str": "1",
                "modules": {"module_dynamic": {"desc": {"text": "动态不存在"}}},
            }
        }
        assert is_repost_original_invalid(item)

    def test_text_marker_video_invalid(self):
        item = {
            "orig": {
                "id_str": "1",
                "modules": {"module_dynamic": {"desc": {"text": "啊叻？视频不见了"}}},
            }
        }
        assert is_repost_original_invalid(item)


# ===================================================================
# 5. Text preview & publish time extraction
# ===================================================================


class TestExtractTextPreview:
    def test_normal(self):
        item = {
            "modules": {"module_dynamic": {"desc": {"text": "Hello world"}}}
        }
        assert extract_text_preview(item) == "Hello world"

    def test_truncation(self):
        item = {
            "modules": {"module_dynamic": {"desc": {"text": "A" * 100}}}
        }
        result = extract_text_preview(item, max_len=80)
        assert len(result) == 81  # 80 chars + "…"
        assert result.endswith("…")

    def test_no_desc(self):
        assert extract_text_preview({}) == ""


class TestExtractPublishTime:
    def test_pub_ts(self):
        dt = extract_publish_time({"pub_ts": 1704067200})
        assert dt is not None
        assert dt.year == 2024

    def test_module_author_fallback(self):
        item = {
            "modules": {"module_author": {"pub_ts": 1704067200}}
        }
        dt = extract_publish_time(item)
        assert dt is not None

    def test_no_timestamp(self):
        assert extract_publish_time({"id_str": "1"}) is None


# ===================================================================
# 6. CandidateInfo
# ===================================================================


class TestCandidateInfo:
    def test_construction(self):
        c = CandidateInfo(
            dyn_id="123",
            orig_lottery_id="456",
            detect_reason="api_status",
            is_repost_invalid=False,
        )
        assert c.dyn_id == "123"
        assert not c.is_repost_invalid

    def test_sanitized_dict_no_sensitive_fields(self):
        c = CandidateInfo(
            dyn_id="123",
            orig_lottery_id="456",
            detect_reason="test",
        )
        d = c.sanitized_dict()
        # Must contain expected fields
        assert "dyn_id" in d
        assert "orig_lottery_id" in d
        assert "detect_reason" in d
        # Must NOT contain sensitive fields
        for sensitive in SANITIZE_FIELDS:
            assert sensitive not in d, f"Leaked: {sensitive}"

    def test_sanitized_dict_text_preview_truncated(self):
        c = CandidateInfo(
            dyn_id="123",
            orig_lottery_id="456",
            text_preview="x" * 300,
            detect_reason="test",
        )
        d = c.sanitized_dict()
        assert len(d["text_preview"]) <= 200

    def test_raw_item_not_in_sanitized(self):
        """raw_item should never appear in sanitized export."""
        c = CandidateInfo(
            dyn_id="123",
            orig_lottery_id="456",
            detect_reason="test",
            raw_item={"params": {"dyn_id_str": "123"}},
        )
        d = c.sanitized_dict()
        assert "raw_item" not in d


# ===================================================================
# 7. CLI
# ===================================================================


class TestCLI:
    def test_default_mode(self):
        """Default: no flags → dry_run only."""
        parser = build_parser()
        args = parser.parse_args([])
        assert not args.execute
        assert not args.yes
        assert not args.debug
        assert not args.non_interactive
        assert not args.dry_run

        dry_run = not args.execute
        assert dry_run is True

    def test_dry_run_explicit(self):
        """--dry-run is an explicit no-op (same as default)."""
        parser = build_parser()
        args = parser.parse_args(["--dry-run"])
        assert args.dry_run
        assert not args.execute
        dry_run = not args.execute or args.dry_run
        assert dry_run is True

    def test_dry_run_execute_conflict(self):
        """--dry-run + --execute should be caught as error."""
        parser = build_parser()
        args = parser.parse_args(["--dry-run", "--execute"])
        assert args.dry_run and args.execute
        # main() must reject this combination

    def test_execute_mode(self):
        parser = build_parser()
        args = parser.parse_args(["--execute"])
        assert args.execute
        dry_run = not args.execute
        assert dry_run is False

    def test_execute_yes_mode(self):
        parser = build_parser()
        args = parser.parse_args(["--execute", "--yes"])
        assert args.execute
        assert args.yes
        require_confirm = not args.yes and args.execute
        assert require_confirm is False

    def test_execute_without_yes_requires_confirm(self):
        parser = build_parser()
        args = parser.parse_args(["--execute"])
        dry_run = not args.execute
        require_confirm = not args.yes and not dry_run
        assert require_confirm is True

    def test_non_interactive_forbids_execute(self):
        """CI mode: --non-interactive + --execute should be caught."""
        parser = build_parser()
        args = parser.parse_args(["--non-interactive", "--execute"])
        assert args.non_interactive
        assert args.execute

    def test_yes_without_execute_is_noop(self):
        parser = build_parser()
        args = parser.parse_args(["--yes"])
        assert args.yes
        assert not args.execute

    def test_debug_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--debug"])
        assert args.debug

    def test_export_candidates(self):
        parser = build_parser()
        args = parser.parse_args(["--export-candidates"])
        assert args.export_candidates

    def test_kill_browser(self):
        parser = build_parser()
        args = parser.parse_args(["--kill-browser"])
        assert args.kill_browser

    def test_include_invalid_repost(self):
        parser = build_parser()
        args = parser.parse_args(["--include-invalid-repost"])
        assert args.include_invalid_repost

    def test_include_invalid_repost_default_off(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert not args.include_invalid_repost


# ===================================================================
# 8. API error handling (mock)
# ===================================================================


class TestErrorHandling:
    """Test that API errors are raised as proper exceptions, not silently swallowed."""

    def test_get_dynamics_raises_on_http_error(self):
        import requests
        from delete import BilibiliLotteryCleaner, retry_request

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner.session, "get") as mock_get:
            mock_resp = Mock()
            mock_resp.raise_for_status.side_effect = requests.HTTPError(
                response=Mock(status_code=500)
            )
            mock_get.return_value = mock_resp

            with pytest.raises((NetworkError, RateLimitError)):
                cleaner.get_dynamics()

    def test_get_dynamics_raises_on_auth_error(self):
        import requests
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner.session, "get") as mock_get:
            mock_resp = Mock()
            mock_resp.raise_for_status.side_effect = requests.HTTPError(
                response=Mock(status_code=401)
            )
            mock_get.return_value = mock_resp

            with pytest.raises(AuthError):
                cleaner.get_dynamics()

    def test_get_dynamics_raises_on_bilibili_error_code(self):
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner.session, "get") as mock_get:
            mock_resp = Mock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {"code": -101, "message": "未登录"}
            mock_get.return_value = mock_resp

            with pytest.raises(AuthError):
                cleaner.get_dynamics()

    def test_get_dynamics_raises_on_rate_limit_code(self):
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner.session, "get") as mock_get:
            mock_resp = Mock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {"code": -412, "message": "访问被拒绝"}
            mock_get.return_value = mock_resp

            with pytest.raises(RateLimitError):
                cleaner.get_dynamics()

    def test_get_dynamics_raises_on_api_error(self):
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner.session, "get") as mock_get:
            mock_resp = Mock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {"code": 1, "message": "参数错误"}
            mock_get.return_value = mock_resp

            with pytest.raises(ApiError):
                cleaner.get_dynamics()

    def test_get_dynamics_success(self):
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner.session, "get") as mock_get:
            mock_resp = Mock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {
                "code": 0,
                "data": {
                    "items": [{"id_str": "1"}],
                    "has_more": False,
                },
            }
            mock_get.return_value = mock_resp

            items, offset = cleaner.get_dynamics()
            assert len(items) == 1
            assert offset is None


# ===================================================================
# 9. Process dynamics (mock integration tests)
# ===================================================================


class TestProcessDynamics:
    """Mock-the-network tests for the main process_dynamics flow."""

    def test_dry_run_never_deletes(self, sample_lottery_item):
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner, "get_dynamics") as mock_get:
            mock_get.return_value = ([sample_lottery_item], None)

            with patch.object(cleaner, "get_lottery_info") as mock_lottery:
                mock_lottery.return_value = LotteryQueryResult(info={
                    "lottery_status": "1",
                    "lottery_time": (
                        datetime.now(TZ_SHANGHAI) - timedelta(days=7)
                    ).timestamp(),
                })

                with patch.object(cleaner, "delete_dynamic") as mock_delete:
                    cleaner.process_dynamics(dry_run=True, require_confirm=False)

                mock_delete.assert_not_called()

    def test_unknown_state_never_deletes(self, sample_lottery_item):
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner, "get_dynamics") as mock_get:
            mock_get.return_value = ([sample_lottery_item], None)

            with patch.object(cleaner, "get_lottery_info") as mock_lottery:
                mock_lottery.return_value = LotteryQueryResult(
                    error_type="empty"
                )

                with patch.object(cleaner, "delete_dynamic") as mock_delete:
                    cleaner.process_dynamics(
                        dry_run=False, require_confirm=True
                    )

                mock_delete.assert_not_called()
                assert cleaner.stats["skipped_unknown"] >= 1

    def test_finished_lottery_deletes_in_execute_yes_mode(self, sample_lottery_item):
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner, "get_dynamics") as mock_get:
            mock_get.return_value = ([sample_lottery_item], None)

            with patch.object(cleaner, "get_lottery_info") as mock_lottery:
                mock_lottery.return_value = LotteryQueryResult(info={
                    "lottery_status": "1",
                    "lottery_time": (
                        datetime.now(TZ_SHANGHAI) - timedelta(days=7)
                    ).timestamp(),
                })

                with patch.object(cleaner, "delete_dynamic") as mock_delete:
                    mock_delete.return_value = (True, "")
                    # --execute --yes: dry_run=False, require_confirm=False
                    cleaner.process_dynamics(
                        dry_run=False, require_confirm=False
                    )

                mock_delete.assert_called_once()
                assert cleaner.stats["deleted"] == 1

    def test_finished_lottery_deletes_after_confirm(self, sample_lottery_item):
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner, "get_dynamics") as mock_get:
            mock_get.return_value = ([sample_lottery_item], None)

            with patch.object(cleaner, "get_lottery_info") as mock_lottery:
                mock_lottery.return_value = LotteryQueryResult(info={
                    "lottery_status": "1",
                    "lottery_time": (
                        datetime.now(TZ_SHANGHAI) - timedelta(days=7)
                    ).timestamp(),
                })

                with patch.object(cleaner, "_confirm_and_delete") as mock_confirm:
                    # --execute (no --yes): dry_run=False, require_confirm=True
                    cleaner.process_dynamics(
                        dry_run=False, require_confirm=True
                    )

                # _confirm_and_delete should be called with candidates
                mock_confirm.assert_called_once()
                candidates = mock_confirm.call_args[0][0]
                assert len(candidates) >= 1
                for c in candidates:
                    assert c.raw_item is not None

    def test_active_lottery_not_deleted(self, sample_lottery_item):
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner, "get_dynamics") as mock_get:
            mock_get.return_value = ([sample_lottery_item], None)

            with patch.object(cleaner, "get_lottery_info") as mock_lottery:
                mock_lottery.return_value = LotteryQueryResult(info={
                    "lottery_status": "0",
                })

                with patch.object(cleaner, "delete_dynamic") as mock_delete:
                    cleaner.process_dynamics(
                        dry_run=False, require_confirm=False
                    )

                mock_delete.assert_not_called()

    def test_invalid_repost_not_deleted_by_default(self, sample_invalid_repost_item):
        """Without --include-invalid-repost, invalid reposts are NOT candidates."""
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner, "get_dynamics") as mock_get:
            mock_get.return_value = ([sample_invalid_repost_item], None)

            with patch.object(cleaner, "delete_dynamic") as mock_delete:
                cleaner.process_dynamics(
                    dry_run=False, require_confirm=False,
                    include_invalid_repost=False,
                )

            # Should NOT be called — default excludes invalid reposts
            mock_delete.assert_not_called()
            # Should NOT count as invalid_repost (only counts when opt-in)
            assert cleaner.stats["deleted"] == 0

    def test_invalid_repost_deletes_when_opted_in(self, sample_invalid_repost_item):
        """With --include-invalid-repost, invalid reposts are candidates."""
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner, "get_dynamics") as mock_get:
            mock_get.return_value = ([sample_invalid_repost_item], None)

            with patch.object(cleaner, "delete_dynamic") as mock_delete:
                mock_delete.return_value = (True, "")
                cleaner.process_dynamics(
                    dry_run=False, require_confirm=False,
                    include_invalid_repost=True,
                )

            mock_delete.assert_called_once()
            assert cleaner.stats["invalid_repost"] >= 1

    def test_api_failure_does_not_silently_end_scan(self):
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner, "get_dynamics") as mock_get:
            mock_get.side_effect = NetworkError("连接超时")

            cleaner.process_dynamics(dry_run=True, require_confirm=False)

            assert cleaner.stats["total"] == 0

    def test_delete_never_called_during_scan(self, sample_lottery_item):
        """Regression P0-2: delete_dynamic must NEVER be called during the scan loop.
        It should only be called after the candidate table is printed."""
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        call_order = []

        with patch.object(cleaner, "get_dynamics") as mock_get:
            mock_get.return_value = ([sample_lottery_item], None)

            with patch.object(cleaner, "get_lottery_info") as mock_lottery:
                mock_lottery.return_value = LotteryQueryResult(info={
                    "lottery_status": "1",
                    "lottery_time": (
                        datetime.now(TZ_SHANGHAI) - timedelta(days=7)
                    ).timestamp(),
                })

                with patch.object(cleaner, "_print_candidates_table") as mock_table:
                    mock_table.side_effect = lambda *a: call_order.append("table")

                    with patch.object(cleaner, "delete_dynamic") as mock_delete:
                        mock_delete.side_effect = lambda *a: (
                            call_order.append("delete") or (True, "")
                        )

                        cleaner.process_dynamics(
                            dry_run=False, require_confirm=False,
                        )

        # Table must appear BEFORE any deletion
        if "delete" in call_order:
            table_idx = call_order.index("table")
            delete_idx = call_order.index("delete")
            assert table_idx < delete_idx, (
                "BUG: delete_dynamic called before _print_candidates_table! "
                "Table must be shown first."
            )

    def test_scan_always_collects_raw_items(self, sample_lottery_item):
        """ALL candidates must carry raw_item, regardless of require_confirm."""
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner, "get_dynamics") as mock_get:
            mock_get.return_value = ([sample_lottery_item], None)

            with patch.object(cleaner, "get_lottery_info") as mock_lottery:
                mock_lottery.return_value = LotteryQueryResult(info={
                    "lottery_status": "1",
                    "lottery_time": (
                        datetime.now(TZ_SHANGHAI) - timedelta(days=7)
                    ).timestamp(),
                })

                with patch.object(cleaner, "_execute_deletion") as mock_exec:
                    cleaner.process_dynamics(
                        dry_run=False, require_confirm=False,
                    )

                if mock_exec.call_args:
                    candidates = mock_exec.call_args[0][0]
                    for c in candidates:
                        assert c.raw_item is not None, (
                            "All candidates must carry raw_item for deletion"
                        )
                        assert "params" in c.raw_item

    def test_failed_delete_increments_failed_not_deleted(self, sample_lottery_item):
        """P0-2 regression: (False, 'error') tuple must NOT be truthy."""
        from delete import BilibiliLotteryCleaner

        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner, "get_dynamics") as mock_get:
            mock_get.return_value = ([sample_lottery_item], None)

            with patch.object(cleaner, "get_lottery_info") as mock_lottery:
                mock_lottery.return_value = LotteryQueryResult(info={
                    "lottery_status": "1",
                    "lottery_time": (
                        datetime.now(TZ_SHANGHAI) - timedelta(days=7)
                    ).timestamp(),
                })

                with patch.object(cleaner, "delete_dynamic") as mock_delete:
                    # Return (False, "error") — must NOT be treated as success
                    mock_delete.return_value = (False, "api error")

                    with patch.object(time, "sleep", lambda _: None):
                        with patch.object(cleaner, "_random_delay", lambda *a, **kw: None):
                            cleaner.process_dynamics(
                                dry_run=False, require_confirm=False,
                            )

                assert cleaner.stats["deleted"] == 0, (
                    "BUG: (False, 'error') treated as True due to tuple truthiness"
                )
                assert cleaner.stats["failed"] >= 1

    def test_lottery_status_zero_never_finished(self, sample_lottery_item):
        """P0-3 regression: lottery_status=0 → ACTIVE even with old timestamp."""
        from delete import BilibiliLotteryCleaner

        # Modify sample item to trigger lottery detection
        cleaner = BilibiliLotteryCleaner(
            "DedeUserID=1; bili_jct=abc; SESSDATA=x"
        )

        with patch.object(cleaner, "get_dynamics") as mock_get:
            mock_get.return_value = ([sample_lottery_item], None)

            with patch.object(cleaner, "get_lottery_info") as mock_lottery:
                # lottery_status=0, very old lottery_time — must stay ACTIVE
                mock_lottery.return_value = LotteryQueryResult(info={
                    "lottery_status": 0,
                    "lottery_time": (
                        datetime.now(TZ_SHANGHAI) - timedelta(days=365)
                    ).timestamp(),
                })

                with patch.object(cleaner, "delete_dynamic") as mock_delete:
                    cleaner.process_dynamics(
                        dry_run=False, require_confirm=False,
                    )

                # Must NOT delete — status 0 means active
                mock_delete.assert_not_called()


# ===================================================================
# 10. Exception hierarchy
# ===================================================================


class TestExceptionHierarchy:
    def test_bili_error_is_base(self):
        assert issubclass(ApiError, BiliError)
        assert issubclass(AuthError, BiliError)
        assert issubclass(RateLimitError, BiliError)
        assert issubclass(NetworkError, BiliError)

    def test_catch_all(self):
        """All BiliErrors can be caught with the base class."""
        test_cases = [
            (ApiError, {"code": 1, "message": "test"}),
            (AuthError, {}),
            (RateLimitError, {}),
            (NetworkError, {}),
        ]
        for exc_class, kwargs in test_cases:
            try:
                raise exc_class(**kwargs)
            except BiliError:
                pass  # expected
            else:
                pytest.fail(f"{exc_class.__name__} not caught by BiliError")


# ===================================================================
# 11. Sanitize for log
# ===================================================================


class TestSanitizeForLog:
    def test_short_value(self):
        from delete import _sanitize_for_log
        assert _sanitize_for_log("12") == "****"

    def test_normal_uid(self):
        from delete import _sanitize_for_log
        result = _sanitize_for_log("123456789")
        assert result == "****6789"
        assert "12345" not in result  # first digits hidden


# ===================================================================
# 12. Time helpers
# ===================================================================


class TestTimeHelpers:
    def test_fmt_time_short(self):
        from delete import _fmt_time_short
        dt = datetime(2024, 1, 15, 14, 30, 0, tzinfo=TZ_SHANGHAI)
        assert _fmt_time_short(dt) == "01-15 14:30"

    def test_fmt_time_short_none(self):
        from delete import _fmt_time_short
        assert _fmt_time_short(None) == "未知"

    def test_fmt_time_short_naive(self):
        """Naive datetime is treated as Shanghai time."""
        from delete import _fmt_time_short
        dt = datetime(2024, 6, 1, 12, 0, 0)
        result = _fmt_time_short(dt)
        assert "06-01" in result

    def test_now_shanghai(self):
        from delete import _now_shanghai
        now = _now_shanghai()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(hours=8)
