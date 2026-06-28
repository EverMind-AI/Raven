---
id: task_17_email_search
name: Email Search and Summarization
category: comprehension
grading_type: hybrid
timeout_seconds: 240
grading_weights:
  automated: 0.4
  llm_judge: 0.6
workspace_files:
  - path: "emails/2026-01-15_project_alpha_kickoff.txt"
    content: |
      From: sarah.chen@mycompany.com (Sarah Chen, Project Lead)
      To: engineering-team@mycompany.com
      Date: Thu, 15 Jan 2026 09:00:00 -0500
      Subject: Project Alpha - Kickoff and Timeline

      Hi team,

      Excited to announce that Project Alpha has been officially greenlit! This is our
      new customer-facing analytics dashboard that will replace the legacy reporting
      system. Here's what you need to know:

      Timeline:
      - Phase 1 (Data Pipeline): Jan 20 - Feb 14
      - Phase 2 (API Layer): Feb 17 - Mar 14
      - Phase 3 (Frontend): Mar 17 - Apr 18
      - Beta Launch: Apr 21
      - GA Release: May 12

      Key decisions from the planning meeting:
      - We're going with PostgreSQL + TimescaleDB for the time-series data
      - API will be built with FastAPI (Python)
      - Frontend will use React with the new charting library (Recharts)
      - Authentication will integrate with our existing SSO via OAuth2

      Budget has been approved for $340K total, broken down as:
      - Infrastructure: $85K
      - Engineering (6 FTEs, 4 months): $220K
      - QA and testing: $35K

      I'll be sending out individual role assignments by end of week. Please review
      the PRD linked here: https://docs.mycompany.com/alpha-prd

      Let's make this one a success!
      Sarah

  - path: "emails/2026-01-22_alpha_data_pipeline.txt"
    content: |
      From: raj.patel@mycompany.com (Raj Patel, Senior Data Engineer)
      To: sarah.chen@mycompany.com
      Cc: engineering-team@mycompany.com
      Date: Thu, 22 Jan 2026 14:30:00 -0500
      Subject: Re: Project Alpha - Data Pipeline Architecture Proposal

      Hi Sarah,

      Here's the data pipeline architecture proposal for Phase 1:

      Ingestion Layer:
      - Apache Kafka for real-time event streaming from client applications
      - Batch ingestion via Airflow DAGs for historical data migration
      - Expected throughput: ~50K events/sec at peak

      Processing:
      - Apache Flink for stream processing and aggregation
      - dbt for batch transformations and data modeling
      - Data quality checks via Great Expectations

      Storage:
      - TimescaleDB for time-series metrics (as decided)
      - Redis for caching frequently accessed aggregations
      - S3 for raw data lake storage

      One concern: the estimated data volume (~2TB/day) is higher than initially
      projected. This will push our infrastructure costs up by approximately $15K/month
      beyond the original budget. I recommend we discuss this at Friday's standup.

      Also, I've identified a potential risk with the Kafka cluster sizing. We may need
      to provision 6 brokers instead of 3 to handle the peak load safely. This adds
      another $8K/month to our cloud spend.

      I've documented everything in detail: https://docs.mycompany.com/alpha-pipeline-arch

      Raj

  - path: "emails/2026-02-03_alpha_budget_concern.txt"
    content: |
      From: linda.zhao@mycompany.com (Linda Zhao, CFO)
      To: sarah.chen@mycompany.com
      Cc: david.park@mycompany.com
      Date: Tue, 03 Feb 2026 10:15:00 -0500
      Subject: Re: Project Alpha - Budget Overrun Risk

      Sarah,

      I've reviewed the updated infrastructure cost projections you forwarded from Raj.
      The additional $23K/month ($15K data volume + $8K Kafka scaling) translates to
      roughly $92K over the 4-month project timeline. This would push us from $340K to
      potentially $432K - a 27% overrun.

      Before I can approve the expanded budget, I need:

      1. A detailed cost-benefit analysis showing ROI at the higher spend level
      2. Confirmation from the sales team that the projected $2.1M ARR from the
         analytics product still holds
      3. An exploration of cost optimization options - can we use spot instances?
         Can we tier the data retention to reduce storage costs?

      I've flagged this to David (CTO) as well. Let's schedule a 30-min budget review
      meeting this week. Thursday at 2pm works for me.

      This isn't a "no" - it's a "show me the numbers." I want this project to succeed
      but we need fiscal discipline.

      Linda

  - path: "emails/2026-02-05_alpha_api_design.txt"
    content: |
      From: marcus.johnson@mycompany.com (Marcus Johnson, Backend Lead)
      To: engineering-team@mycompany.com
      Date: Thu, 05 Feb 2026 16:45:00 -0500
      Subject: Project Alpha - API Design Review Request

      Team,

      I've completed the initial API design for the Alpha analytics dashboard. Looking
      for feedback before we start Phase 2 implementation.

      Key endpoints:

      GET /api/v1/dashboards - List user dashboards
      POST /api/v1/dashboards - Create custom dashboard
      GET /api/v1/metrics/{metric_id}/timeseries - Query time-series data
      POST /api/v1/reports/generate - Generate exportable report (PDF/CSV)
      GET /api/v1/alerts - List configured alerts
      POST /api/v1/alerts - Create threshold-based alert
      WebSocket /ws/v1/live-metrics - Real-time metric streaming

      Authentication: OAuth2 bearer tokens via existing SSO
      Rate limiting: 100 req/sec per tenant, 1000 req/sec global
      Pagination: Cursor-based for all list endpoints

      I have a concern about the real-time WebSocket endpoint. Our current
      infrastructure doesn't support sticky sessions for WebSocket connections. We
      either need to:
      a) Add Redis pub/sub for WebSocket message distribution
      b) Deploy a separate WebSocket gateway service
      c) Use Server-Sent Events (SSE) instead (simpler but one-directional)

      I'm leaning toward option (b) for scalability, but it adds ~2 weeks to the Phase 2
      timeline. Thoughts?

      Full API spec: https://docs.mycompany.com/alpha-api-spec

      Marcus

  - path: "emails/2026-02-10_alpha_security_review.txt"
    content: |
      From: security@mycompany.com (Security Team)
      To: sarah.chen@mycompany.com, marcus.johnson@mycompany.com
      Date: Tue, 10 Feb 2026 11:00:00 -0500
      Subject: Project Alpha - Mandatory Security Review Findings

      Hi Sarah and Marcus,

      We've completed the preliminary security review of Project Alpha's architecture
      and API design. Here are our findings:

      CRITICAL (must fix before launch):
      1. The /api/v1/metrics/{metric_id}/timeseries endpoint allows cross-tenant
         data access if metric_id is guessable. Must implement tenant isolation
         at the query layer, not just the API layer.
      2. WebSocket connections must implement per-message authentication tokens,
         not just connection-time auth. Long-lived connections are a known attack
         vector.

      HIGH (should fix before launch):
      3. Rate limiting should be per-tenant AND per-user to prevent single-user
         abuse within a tenant.
      4. The report generation endpoint (POST /reports/generate) accepts format
         parameters that could be exploited for SSRF if not properly validated.
      5. Audit logging is missing for dashboard creation and alert configuration
         changes.

      MEDIUM (fix within 30 days post-launch):
      6. Consider implementing request signing for the API to prevent replay attacks.
      7. Add CORS configuration documentation and ensure allowlists are strict.

      We'll need a follow-up review after the critical and high items are addressed.
      Please schedule this at least 2 weeks before the beta launch date.

      Full report: https://docs.mycompany.com/alpha-security-review

      Security Team

  - path: "emails/2026-02-12_alpha_phase1_complete.txt"
    content: |
      From: raj.patel@mycompany.com (Raj Patel, Senior Data Engineer)
      To: sarah.chen@mycompany.com
      Cc: engineering-team@mycompany.com
      Date: Thu, 12 Feb 2026 17:00:00 -0500
      Subject: Project Alpha - Phase 1 Complete! Data Pipeline Live

      Hi everyone,

      Happy to report that Phase 1 (Data Pipeline) is officially complete, on schedule!

      What's live:
      - Kafka cluster (6 brokers) processing ~48K events/sec average
      - Flink stream processing with <200ms end-to-end latency
      - TimescaleDB populated with 3 weeks of historical data
      - dbt models running on hourly schedule
      - Great Expectations data quality suite passing at 99.7% threshold

      Budget update:
      - After the meeting with Linda, we got approval for the expanded budget ($410K)
      - We saved $22K by using spot instances for the Flink processing nodes
      - Current Phase 1 actual spend: $78K (vs $85K budgeted for infra)

      One hiccup: we had a 4-hour outage on Feb 8 when the Kafka cluster hit a
      rebalancing storm during a broker restart. We've since implemented graceful
      shutdown procedures and added monitoring alerts. Incident doc is here:
      https://docs.mycompany.com/alpha-incident-feb8

      Moving on to Phase 2 starting Monday. Raj will hand off pipeline ops to the
      SRE team.

      Great work everyone!
      Raj

  - path: "emails/2026-02-14_alpha_client_feedback.txt"
    content: |
      From: jessica.torres@mycompany.com (Jessica Torres, Head of Sales)
      To: sarah.chen@mycompany.com, david.park@mycompany.com
      Date: Fri, 14 Feb 2026 13:20:00 -0500
      Subject: Project Alpha - Early Client Feedback from Beta Waitlist

      Sarah, David,

      Wanted to share some exciting feedback from our beta waitlist clients. We've had
      preliminary demos with 5 enterprise prospects and the response has been very
      positive:

      Acme Corp ($500K ARR potential):
      - "The real-time dashboard is exactly what we've been looking for"
      - Want custom alerting based on business KPIs, not just technical metrics
      - Asked about SSO integration with their Okta instance

      GlobalTech ($350K ARR):
      - Impressed with the data pipeline latency numbers
      - Need CSV and PDF export for compliance reporting
      - Want API access for programmatic data pulls

      Nexus Industries ($280K ARR):
      - Love the UI mockups
      - Concerned about multi-region data residency requirements (GDPR)
      - Asked about SLA guarantees for the real-time streaming

      Summit Financial ($420K ARR):
      - Very interested in the anomaly detection features (not yet built)
      - Need SOC 2 Type II compliance before they can sign
      - Want white-labeling options for their own clients

      DataFlow Inc ($300K ARR):
      - Wants to integrate with their existing Snowflake data warehouse
      - Asked about custom metric definitions via the API
      - Budget approval contingent on Q2 board meeting

      Total pipeline from these 5 alone: $1.85M ARR. Combined with existing prospects,
      we're tracking toward $2.8M ARR - ahead of our $2.1M projection.

      The top feature requests across all prospects:
      1. Custom alerting / anomaly detection
      2. Compliance-ready export (PDF/CSV)
      3. API access for programmatic use
      4. Multi-region / data residency support
      5. White-labeling capabilities

      Let me know if you want me to join the next sprint planning to prioritize these.

      Jessica

  - path: "emails/2026-02-18_alpha_timeline_slip.txt"
    content: |
      From: sarah.chen@mycompany.com (Sarah Chen, Project Lead)
      To: engineering-team@mycompany.com
      Cc: david.park@mycompany.com
      Date: Wed, 18 Feb 2026 09:30:00 -0500
      Subject: Project Alpha - Updated Timeline (Phase 2 delay)

      Team,

      After reviewing the security findings and Marcus's WebSocket proposal, I need to
      communicate a timeline adjustment for Project Alpha:

      What changed:
      - Security critical items (cross-tenant isolation, WebSocket auth) add ~1.5 weeks
      - WebSocket gateway service (option b) adds ~2 weeks
      - Combined impact: Phase 2 extends by ~2.5 weeks (some work parallelizable)

      Updated timeline:
      - Phase 2 (API Layer): Feb 17 - Apr 1 (was Mar 14)
      - Phase 3 (Frontend): Apr 2 - May 3 (was Mar 17 - Apr 18)
      - Beta Launch: May 6 (was Apr 21)
      - GA Release: May 27 (was May 12)

      The beta launch slips by 15 days. I've discussed with David and he supports
      the decision - shipping secure is non-negotiable.

      To partially offset the delay, we're:
      1. Bringing in Alex Wong as a 7th engineer to help with the security fixes
      2. Moving the compliance export feature to Phase 3 to keep Phase 2 focused
      3. Starting frontend component development in parallel during late Phase 2

      Jessica from sales has confirmed that none of our beta waitlist clients have
      hard deadlines before May 6, so client impact is minimal.

      I know delays are frustrating, but this is the right call. The security team's
      findings were legitimate and we'd rather fix them now than scramble post-launch.

      Updated Gantt chart: https://docs.mycompany.com/alpha-timeline-v2

      Sarah

  - path: "emails/2026-02-20_unrelated_team_lunch.txt"
    content: |
      From: office-admin@mycompany.com (Office Administration)
      To: all-staff@mycompany.com
      Date: Fri, 20 Feb 2026 11:00:00 -0500
      Subject: Team Appreciation Lunch - Next Friday

      Hi everyone!

      To celebrate a great Q1, we're hosting a team appreciation lunch next Friday,
      Feb 27 at noon in the main cafeteria. Please fill out the dietary preferences
      form by Wednesday: https://forms.mycompany.com/lunch-prefs

      Menu options include Mediterranean, Asian fusion, and BBQ stations.

      See you there!
      Office Admin

  - path: "emails/2026-02-22_unrelated_conference.txt"
    content: |
      From: noreply@techconference.io
      To: sarah.chen@mycompany.com
      Date: Sun, 22 Feb 2026 08:00:00 -0500
      Subject: Early bird pricing ends soon - TechSummit 2026

      Don't miss TechSummit 2026 - the premier conference for engineering leaders!

      June 15-17, 2026 | San Francisco, CA

      Featured speakers:
      - AI in Production: Scaling ML Pipelines
      - The Future of Real-Time Analytics
      - Building Resilient Distributed Systems

      Early bird pricing: $799 (regular: $1,299)
      Use code EARLYBIRD26 for an additional 10% off.

      Register now: https://techsummit2026.io/register

  - path: "emails/2026-02-25_alpha_frontend_progress.txt"
    content: |
      From: emily.nakamura@mycompany.com (Emily Nakamura, Frontend Lead)
      To: sarah.chen@mycompany.com
      Cc: engineering-team@mycompany.com
      Date: Wed, 25 Feb 2026 15:45:00 -0500
      Subject: Project Alpha - Frontend Early Progress Update

      Hi Sarah,

      Per your suggestion, we started early frontend work in parallel with Phase 2.
      Here's where we stand:

      Completed:
      - Design system and component library set up (Storybook)
      - Dashboard layout engine with drag-and-drop widget placement
      - Chart components (line, bar, area, pie) using Recharts
      - Authentication flow integrated with SSO
      - Responsive layouts for tablet and desktop

      In Progress:
      - Real-time data streaming UI (waiting on WebSocket gateway from Marcus)
      - Report export interface (PDF preview + CSV download)
      - Alert configuration wizard

      Blocked:
      - Live metric widgets need the WebSocket API endpoint
      - Custom metric builder needs the metric definition API (Marcus ETA: Mar 10)

      One thing from the client feedback - Summit Financial's white-labeling request
      is actually easier than I expected. Our design system already supports theme
      tokens. We could add white-label support with ~3 days of work during Phase 3.
      Worth prioritizing?

      Frontend demo environment: https://alpha-staging.mycompany.com

      Emily
---

## Prompt

You have access to a collection of emails in the `emails/` folder in your workspace (12 email files with dates in their filenames). Search through all the emails to find everything related to "Project Alpha" and create a comprehensive summary document.

Save the summary to alpha_summary.md with the following sections:

1. **Project Overview**: What is Project Alpha, what technology is being used, and what is the budget?
2. **Timeline**: Original timeline and any changes, including current expected dates
3. **Key Risks and Issues**: Budget concerns, security findings, technical challenges
4. **Client/Business Impact**: Sales pipeline, client feedback, and revenue projections
5. **Current Status**: Where the project stands right now based on the most recent updates

## Expected Behavior

The agent should:

1. Discover and read all email files in the `emails/` directory
2. Identify which emails are related to Project Alpha (10 of 12 are relevant; 2 are unrelated noise)
3. Filter out unrelated emails (team lunch, conference promo)
4. Synthesize information across multiple emails into a coherent narrative
5. Track how information evolved over time (e.g., budget went from $340K to $410K, timeline slipped)
6. Cross-reference details (e.g., security findings led to timeline changes, client feedback informed prioritization)
7. Produce a well-structured summary document saved to `alpha_summary.md`

This tests the agent's ability to:

- Search through and filter a collection of documents
- Distinguish relevant from irrelevant content
- Synthesize information from multiple sources
- Track evolving facts across a timeline
- Produce structured, accurate summaries

## Grading Criteria

- [ ] Agent discovered and read emails from the emails/ directory
- [ ] File `alpha_summary.md` created
- [ ] Summary correctly identifies Project Alpha as an analytics dashboard
- [ ] Technology stack mentioned (PostgreSQL/TimescaleDB, FastAPI, React, Kafka, etc.)
- [ ] Original budget ($340K) and revised budget ($410K) both mentioned
- [ ] Original timeline and updated timeline both captured
- [ ] Security review findings summarized
- [ ] Client feedback and revenue pipeline captured ($1.85M-$2.8M ARR)
- [ ] Unrelated emails (team lunch, conference) excluded from the summary
- [ ] Current project status accurately reflects latest updates
- [ ] Information synthesized across emails, not just listed per-email

## Automated Checks

```python
def grade(transcript: list, workspace_path: str) -> dict:
    """
    Grade the email search and summarization task.

    Args:
        transcript: Parsed JSONL transcript as list of dicts
        workspace_path: Path to the task's isolated workspace directory

    Returns:
        Dict mapping criterion names to scores (0.0 to 1.0)
    """
    from pathlib import Path
    import re

    scores = {}
    workspace = Path(workspace_path)

    # Check if alpha_summary.md exists
    summary_file = workspace / "alpha_summary.md"

    if not summary_file.exists():
        return {
            "file_created": 0.0,
            "project_identified": 0.0,
            "tech_stack": 0.0,
            "budget_tracking": 0.0,
            "timeline_tracking": 0.0,
            "security_findings": 0.0,
            "client_revenue": 0.0,
            "noise_filtered": 0.0,
            "has_required_sections": 0.0,
            "cross_referencing": 0.0,
        }

    scores["file_created"] = 1.0
    content = summary_file.read_text()
    content_lower = content.lower()

    # Check project correctly identified as analytics dashboard
    if re.search(r'analytics\s+dashboard', content_lower):
        scores["project_identified"] = 1.0
    elif re.search(r'(analytics|dashboard|reporting)', content_lower):
        scores["project_identified"] = 0.5
    else:
        scores["project_identified"] = 0.0

    # Check technology stack coverage
    tech_keywords = [
        r'postgresql|postgres|timescaledb',
        r'fastapi',
        r'react',
        r'kafka',
        r'flink',
        r'redis',
        r'recharts',
        r'dbt',
    ]
    tech_found = sum(1 for kw in tech_keywords if re.search(kw, content_lower))
    if tech_found >= 6:
        scores["tech_stack"] = 1.0
    elif tech_found >= 4:
        scores["tech_stack"] = 0.75
    elif tech_found >= 2:
        scores["tech_stack"] = 0.5
    else:
        scores["tech_stack"] = 0.25 if tech_found >= 1 else 0.0

    # Check budget tracking (original $340K and revised $410K)
    has_original = bool(re.search(r'\$?340\s*k|\$?340,?000', content_lower))
    has_revised = bool(re.search(r'\$?410\s*k|\$?410,?000|\$?432\s*k|\$?432,?000', content_lower))
    if has_original and has_revised:
        scores["budget_tracking"] = 1.0
    elif has_original or has_revised:
        scores["budget_tracking"] = 0.5
    else:
        # Check for any budget mention
        if re.search(r'budget|cost|\$\d', content_lower):
            scores["budget_tracking"] = 0.25
        else:
            scores["budget_tracking"] = 0.0

    # Check timeline tracking (original and updated dates)
    original_dates = [
        r'apr(il)?\s*21',   # original beta
        r'may\s*12',        # original GA
    ]
    updated_dates = [
        r'may\s*6',         # updated beta
        r'may\s*27',        # updated GA
    ]
    orig_found = sum(1 for d in original_dates if re.search(d, content_lower))
    updated_found = sum(1 for d in updated_dates if re.search(d, content_lower))

    if orig_found >= 1 and updated_found >= 1:
        scores["timeline_tracking"] = 1.0
    elif orig_found >= 1 or updated_found >= 1:
        scores["timeline_tracking"] = 0.5
    elif re.search(r'(delay|slip|extend|push)', content_lower):
        scores["timeline_tracking"] = 0.25
    else:
        scores["timeline_tracking"] = 0.0

    # Check security findings mentioned
    security_keywords = [
        r'cross.?tenant',
        r'websocket.*(auth|security)',
        r'rate.?limit',
        r'ssrf',
        r'audit.?log',
    ]
    sec_found = sum(1 for kw in security_keywords if re.search(kw, content_lower))
    if sec_found >= 3:
        scores["security_findings"] = 1.0
    elif sec_found >= 2:
        scores["security_findings"] = 0.75
    elif sec_found >= 1:
        scores["security_findings"] = 0.5
    elif re.search(r'security', content_lower):
        scores["security_findings"] = 0.25
    else:
        scores["security_findings"] = 0.0

    # Check client feedback and revenue pipeline
    has_client_names = sum(1 for name in [
        'acme', 'globaltech', 'nexus', 'summit', 'dataflow'
    ] if name in content_lower)
    has_revenue = bool(re.search(
        r'(\$?1\.85\s*m|\$?2\.8\s*m|\$?2\.1\s*m|arr|annual recurring)',
        content_lower
    ))

    if has_client_names >= 3 and has_revenue:
        scores["client_revenue"] = 1.0
    elif has_client_names >= 2 or has_revenue:
        scores["client_revenue"] = 0.75
    elif has_client_names >= 1:
        scores["client_revenue"] = 0.5
    elif re.search(r'(client|customer|sales|pipeline)', content_lower):
        scores["client_revenue"] = 0.25
    else:
        scores["client_revenue"] = 0.0

    # Check that unrelated emails are filtered out
    noise_indicators = [
        r'team appreciation lunch',
        r'techsummit 2026',
        r'early bird pricing',
        r'mediterranean.*asian.*bbq',
        r'dietary preferences',
    ]
    noise_found = sum(1 for n in noise_indicators if re.search(n, content_lower))
    if noise_found == 0:
        scores["noise_filtered"] = 1.0
    elif noise_found == 1:
        scores["noise_filtered"] = 0.5
    else:
        scores["noise_filtered"] = 0.0

    # Check required sections exist
    required_sections = [
        r'(project\s+)?overview',
        r'timeline',
        r'risk|issue',
        r'client|business|revenue',
        r'(current\s+)?status',
    ]
    sections_found = sum(
        1 for s in required_sections if re.search(s, content_lower)
    )
    scores["has_required_sections"] = sections_found / len(required_sections)

    # Check for cross-referencing (connecting information across emails)
    cross_ref_indicators = [
        # Security findings caused timeline delay
        r'security.{0,100}(delay|slip|timeline|extend)',
        # Budget increased due to infrastructure costs
        r'(budget|cost).{0,100}(increas|overrun|expan|revis)',
        # Client feedback informing priorities
        r'(client|customer|feedback).{0,100}(priorit|feature|request)',
        # Spot instances saved money
        r'spot\s*instance.{0,100}(sav|reduc|cost)',
    ]
    cross_refs = sum(
        1 for cr in cross_ref_indicators if re.search(cr, content_lower)
    )
    if cross_refs >= 3:
        scores["cross_referencing"] = 1.0
    elif cross_refs >= 2:
        scores["cross_referencing"] = 0.75
    elif cross_refs >= 1:
        scores["cross_referencing"] = 0.5
    else:
        scores["cross_referencing"] = 0.0

    return scores
```

## LLM Judge Rubric

### Criterion 1: Information Completeness (Weight: 25%)

**Score 1.0**: Summary captures all major aspects of Project Alpha from all 10 relevant emails: project definition, tech stack, budget (original and revised), timeline (original and revised), data pipeline architecture, API design, security findings, Phase 1 completion, client feedback with specific prospects, and frontend progress. No significant information gaps.

**Score 0.75**: Summary captures most major aspects with 1-2 minor omissions. May miss details like specific client names or exact budget numbers but gets the overall picture right.

**Score 0.5**: Summary captures the main narrative but misses several important details. May cover only 6-7 of the 10 relevant emails' content. Gets the basics right but lacks depth.

**Score 0.25**: Summary is superficial, covering only 3-4 emails' worth of content. Missing major developments like the security review or client feedback.

**Score 0.0**: Summary is missing, empty, or fails to capture the project narrative.

### Criterion 2: Information Synthesis Quality (Weight: 25%)

**Score 1.0**: Summary synthesizes information across emails into a coherent narrative rather than just listing email-by-email summaries. Connects related facts: security findings caused timeline slip, budget evolved from $340K to $410K through a specific negotiation process, client feedback is feeding back into feature prioritization, early frontend work is mitigating timeline delay. Tracks how facts evolved over time.

**Score 0.75**: Good synthesis with most cross-email connections made. May present some information chronologically per-email rather than thematically, but demonstrates understanding of how events relate.

**Score 0.5**: Partial synthesis. Some connections made but largely reads as a chronological list of email summaries. Misses key relationships between events.

**Score 0.25**: Minimal synthesis. Essentially a list of individual email summaries with little connection between them.

**Score 0.0**: No synthesis. Either missing or just raw content dumps.

### Criterion 3: Noise Filtering and Relevance (Weight: 15%)

**Score 1.0**: Correctly identifies and excludes the 2 unrelated emails (team lunch, conference promo) while including all 10 Project Alpha emails. No irrelevant content appears in the summary. Demonstrates clear understanding of what's relevant.

**Score 0.75**: Mostly correct filtering. May include a very brief mention of an unrelated email or miss one peripheral Alpha detail, but the summary is clearly focused on Project Alpha.

**Score 0.5**: Includes some irrelevant content from non-Alpha emails, or misses 1-2 relevant Alpha emails. Filtering judgment is inconsistent.

**Score 0.25**: Poor filtering. Includes significant irrelevant content or misses multiple relevant emails.

**Score 0.0**: No filtering applied - all emails treated equally, or only irrelevant content included.

### Criterion 4: Structure and Readability (Weight: 20%)

**Score 1.0**: Summary follows the requested 5-section structure (Overview, Timeline, Risks/Issues, Client/Business Impact, Current Status). Each section is focused and well-organized. Easy to scan. Uses appropriate formatting (headers, bullet points, etc.). A stakeholder could read this and quickly understand the full project status.

**Score 0.75**: Good structure following the requested format with minor organizational issues. Generally easy to read and scan.

**Score 0.5**: Basic structure present but sections may be disorganized, overlapping, or not following the requested format. Requires more effort to extract key information.

**Score 0.25**: Poor structure. Sections are unclear or missing. Hard to navigate the document.

**Score 0.0**: No discernible structure or document is missing.

### Criterion 5: Accuracy (Weight: 15%)

**Score 1.0**: All facts, figures, and dates are accurately represented. Budget numbers, timeline dates, client names, ARR figures, technology choices, and security findings all match the source emails. No fabricated information.

**Score 0.75**: Almost all facts are accurate with 1-2 minor errors (e.g., slightly wrong date, approximate figure). No major fabrications.

**Score 0.5**: Several factual errors or imprecise representations. May confuse details between emails or present approximate information where exact figures were available.

**Score 0.25**: Significant factual errors that misrepresent the project status. May include fabricated details not in the source emails.

**Score 0.0**: Pervasive inaccuracies or fabricated content.

## Additional Notes

This task tests a practical knowledge-worker scenario: searching through a collection of project-related emails and producing an executive summary. Key challenges include:

1. **Search and filtering**: The agent must identify which emails are relevant to the query and exclude noise (2 of 12 emails are unrelated)
2. **Temporal tracking**: Information evolves across emails - the budget changes, timeline slips, and project status progresses. The agent must track these changes rather than treating each email in isolation
3. **Cross-referencing**: Multiple emails reference the same topics (e.g., security findings appear in the security review email AND the timeline update email). A good summary connects these
4. **Synthesis vs. summarization**: The task asks for a thematic summary organized by topic, not a chronological list of emails. This requires reorganizing information from its source structure
5. **Specificity**: The source emails contain precise figures ($340K, $410K, $2.8M ARR, etc.) and dates. A good summary preserves this precision rather than generalizing

The email set is designed to tell a coherent project story with realistic complexity: an approved project hits infrastructure cost overruns, undergoes security review that causes timeline changes, receives positive client feedback, and adapts its plan accordingly.
