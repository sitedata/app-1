import argparse
from dataclasses import dataclass
from time import sleep

import arrow
from arrow import Arrow

from app import s3
from app.api.views.apple import verify_receipt
from app.config import (
    IGNORED_EMAILS,
    ADMIN_EMAIL,
    MACAPP_APPLE_API_SECRET,
    APPLE_API_SECRET,
)
from app.email_utils import (
    send_email,
    send_trial_end_soon_email,
    render,
    email_domain_can_be_used_as_mailbox,
)
from app.extensions import db
from app.log import LOG
from app.models import (
    Subscription,
    User,
    Alias,
    EmailLog,
    CustomDomain,
    Client,
    ManualSubscription,
    RefusedEmail,
    AppleSubscription,
    Mailbox,
    Monitoring,
)
from server import create_app


def notify_trial_end():
    for user in User.query.filter(
        User.activated == True, User.trial_end.isnot(None), User.lifetime == False
    ).all():
        if user.in_trial() and arrow.now().shift(
            days=3
        ) > user.trial_end >= arrow.now().shift(days=2):
            LOG.d("Send trial end email to user %s", user)
            send_trial_end_soon_email(user)


def delete_refused_emails():
    for refused_email in RefusedEmail.query.filter(RefusedEmail.deleted == False).all():
        if arrow.now().shift(days=1) > refused_email.delete_at >= arrow.now():
            LOG.d("Delete refused email %s", refused_email)
            if refused_email.path:
                s3.delete(refused_email.path)

            s3.delete(refused_email.full_report_path)

            # do not set path and full_report_path to null
            # so we can check later that the files are indeed deleted
            refused_email.deleted = True
            db.session.commit()

    LOG.d("Finish delete_refused_emails")


def notify_premium_end():
    """sent to user who has canceled their subscription and who has their subscription ending soon"""
    for sub in Subscription.query.filter(Subscription.cancelled == True).all():
        if (
            arrow.now().shift(days=3).date()
            > sub.next_bill_date
            >= arrow.now().shift(days=2).date()
        ):
            user = sub.user
            LOG.d(f"Send subscription ending soon email to user {user}")

            send_email(
                user.email,
                f"Your subscription will end soon {user.name}",
                render(
                    "transactional/subscription-end.txt",
                    user=user,
                    next_bill_date=sub.next_bill_date.strftime("%Y-%m-%d"),
                ),
                render(
                    "transactional/subscription-end.html",
                    user=user,
                    next_bill_date=sub.next_bill_date.strftime("%Y-%m-%d"),
                ),
            )


def notify_manual_sub_end():
    for manual_sub in ManualSubscription.query.all():
        need_reminder = False
        if arrow.now().shift(days=14) > manual_sub.end_at > arrow.now().shift(days=13):
            need_reminder = True
        elif arrow.now().shift(days=4) > manual_sub.end_at > arrow.now().shift(days=3):
            need_reminder = True

        if need_reminder:
            user = manual_sub.user
            LOG.debug("Remind user %s that their manual sub is ending soon", user)
            send_email(
                user.email,
                f"Your trial will end soon {user.name}",
                render(
                    "transactional/manual-subscription-end.txt",
                    name=user.name,
                    user=user,
                    manual_sub=manual_sub,
                ),
                render(
                    "transactional/manual-subscription-end.html",
                    name=user.name,
                    user=user,
                    manual_sub=manual_sub,
                ),
            )


def poll_apple_subscription():
    """Poll Apple API to update AppleSubscription"""
    # todo: only near the end of the subscription
    for apple_sub in AppleSubscription.query.all():
        user = apple_sub.user
        verify_receipt(apple_sub.receipt_data, user, APPLE_API_SECRET)
        verify_receipt(apple_sub.receipt_data, user, MACAPP_APPLE_API_SECRET)

    LOG.d("Finish poll_apple_subscription")


@dataclass
class Stats:
    nb_user: int
    nb_alias: int

    nb_forward: int
    nb_block: int
    nb_reply: int
    nb_bounced: int
    nb_spam: int

    nb_custom_domain: int
    nb_app: int

    nb_premium: int
    nb_apple_premium: int


def stats_before(moment: Arrow) -> Stats:
    """return the stats before a specific moment, ignoring all stats come from users in IGNORED_EMAILS
    """
    # nb user
    q = User.query
    for ie in IGNORED_EMAILS:
        q = q.filter(~User.email.contains(ie), User.created_at < moment)

    nb_user = q.count()
    LOG.d("total number user %s", nb_user)

    # nb alias
    q = db.session.query(Alias, User).filter(
        Alias.user_id == User.id, Alias.created_at < moment
    )
    for ie in IGNORED_EMAILS:
        q = q.filter(~User.email.contains(ie))

    nb_alias = q.count()
    LOG.d("total number alias %s", nb_alias)

    # email log stats
    q = (
        db.session.query(EmailLog)
        .join(User, EmailLog.user_id == User.id)
        .filter(EmailLog.created_at < moment,)
    )
    for ie in IGNORED_EMAILS:
        q = q.filter(~User.email.contains(ie))

    nb_spam = nb_bounced = nb_forward = nb_block = nb_reply = 0
    for email_log in q.yield_per(500):
        if email_log.bounced:
            nb_bounced += 1
        elif email_log.is_spam:
            nb_spam += 1
        elif email_log.is_reply:
            nb_reply += 1
        elif email_log.blocked:
            nb_block += 1
        else:
            nb_forward += 1

    LOG.d(
        "nb_forward %s, nb_block %s, nb_reply %s, nb_bounced %s, nb_spam %s",
        nb_forward,
        nb_block,
        nb_reply,
        nb_bounced,
        nb_spam,
    )

    nb_premium = Subscription.query.filter(
        Subscription.created_at < moment, Subscription.cancelled == False
    ).count()
    nb_apple_premium = AppleSubscription.query.filter(
        AppleSubscription.created_at < moment
    ).count()

    nb_custom_domain = CustomDomain.query.filter(
        CustomDomain.created_at < moment
    ).count()

    nb_app = Client.query.filter(Client.created_at < moment).count()

    data = locals()
    # to keep only Stats field
    data = {
        k: v
        for (k, v) in data.items()
        if k in vars(Stats)["__dataclass_fields__"].keys()
    }
    return Stats(**data)


def increase_percent(old, new) -> str:
    if old == 0:
        return "N/A"

    increase = (new - old) / old * 100
    return f"{increase:.1f}%"


def stats():
    """send admin stats everyday"""
    if not ADMIN_EMAIL:
        # nothing to do
        return

    stats_today = stats_before(arrow.now())
    stats_yesterday = stats_before(arrow.now().shift(days=-1))

    nb_user_increase = increase_percent(stats_yesterday.nb_user, stats_today.nb_user)
    nb_alias_increase = increase_percent(stats_yesterday.nb_alias, stats_today.nb_alias)
    nb_forward_increase = increase_percent(
        stats_yesterday.nb_forward, stats_today.nb_forward
    )

    today = arrow.now().format()

    send_email(
        ADMIN_EMAIL,
        subject=f"SimpleLogin Stats for {today}, {nb_user_increase} users, {nb_alias_increase} aliases, {nb_forward_increase} forwards",
        plaintext="",
        html=f"""
Stats for {today} <br>

nb_user: {stats_today.nb_user} - {increase_percent(stats_yesterday.nb_user, stats_today.nb_user)}  <br>
nb_premium: {stats_today.nb_premium} - {increase_percent(stats_yesterday.nb_premium, stats_today.nb_premium)}  <br>
nb_apple_premium: {stats_today.nb_apple_premium} - {increase_percent(stats_yesterday.nb_apple_premium, stats_today.nb_apple_premium)}  <br>
nb_alias: {stats_today.nb_alias} - {increase_percent(stats_yesterday.nb_alias, stats_today.nb_alias)}  <br>

nb_forward: {stats_today.nb_forward} - {increase_percent(stats_yesterday.nb_forward, stats_today.nb_forward)}  <br>
nb_reply: {stats_today.nb_reply} - {increase_percent(stats_yesterday.nb_reply, stats_today.nb_reply)}  <br>
nb_block: {stats_today.nb_block} - {increase_percent(stats_yesterday.nb_block, stats_today.nb_block)}  <br>
nb_bounced: {stats_today.nb_bounced} - {increase_percent(stats_yesterday.nb_bounced, stats_today.nb_bounced)}  <br>
nb_spam: {stats_today.nb_spam} - {increase_percent(stats_yesterday.nb_spam, stats_today.nb_spam)}  <br>

nb_custom_domain: {stats_today.nb_custom_domain} - {increase_percent(stats_yesterday.nb_custom_domain, stats_today.nb_custom_domain)}  <br>
nb_app: {stats_today.nb_app} - {increase_percent(stats_yesterday.nb_app, stats_today.nb_app)}  <br>
    """,
    )


def sanity_check():
    """
    #TODO: investigate why DNS sometimes not working
    Different sanity checks
    - detect if there's mailbox that's using a invalid domain
    """
    for mailbox in Mailbox.filter_by(verified=True).all():
        # hack to not query DNS too often
        sleep(1)
        if not email_domain_can_be_used_as_mailbox(mailbox.email):
            mailbox.nb_failed_checks += 1
            # alert if too much fail
            if mailbox.nb_failed_checks > 10:
                log_func = LOG.exception
            else:
                log_func = LOG.warning

            log_func(
                "issue with mailbox %s domain. #alias %s, nb email log %s",
                mailbox,
                mailbox.nb_alias(),
                mailbox.nb_email_log(),
            )
        else:  # reset nb check
            mailbox.nb_failed_checks = 0

    db.session.commit()

    for user in User.filter_by(activated=True).all():
        if user.email.lower() != user.email:
            LOG.exception("%s does not have lowercase email", user)

    for mailbox in Mailbox.filter_by(verified=True).all():
        if mailbox.email.lower() != mailbox.email:
            LOG.exception("%s does not have lowercase email", mailbox)

    LOG.d("Finish sanity check")


def delete_old_monitoring():
    """
    Delete old monitoring records
    """
    max_time = arrow.now().shift(days=-30)
    nb_row = Monitoring.query.filter(Monitoring.created_at < max_time).delete()
    db.session.commit()
    LOG.d("delete monitoring records older than %s, nb row %s", max_time, nb_row)


if __name__ == "__main__":
    LOG.d("Start running cronjob")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-j",
        "--job",
        help="Choose a cron job to run",
        type=str,
        choices=[
            "stats",
            "notify_trial_end",
            "notify_manual_subscription_end",
            "notify_premium_end",
            "delete_refused_emails",
            "poll_apple_subscription",
            "sanity_check",
            "delete_old_monitoring",
        ],
    )
    args = parser.parse_args()

    app = create_app()

    with app.app_context():
        if args.job == "stats":
            LOG.d("Compute Stats")
            stats()
        elif args.job == "notify_trial_end":
            LOG.d("Notify users with trial ending soon")
            notify_trial_end()
        elif args.job == "notify_manual_subscription_end":
            LOG.d("Notify users with manual subscription ending soon")
            notify_manual_sub_end()
        elif args.job == "notify_premium_end":
            LOG.d("Notify users with premium ending soon")
            notify_premium_end()
        elif args.job == "delete_refused_emails":
            LOG.d("Deleted refused emails")
            delete_refused_emails()
        elif args.job == "poll_apple_subscription":
            LOG.d("Poll Apple Subscriptions")
            poll_apple_subscription()
        elif args.job == "sanity_check":
            LOG.d("Check data consistency")
            sanity_check()
        elif args.job == "delete_old_monitoring":
            LOG.d("Delete old monitoring records")
            delete_old_monitoring()
