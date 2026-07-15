from app.clients.jira import strip_common_label

def generate_weekly_chart(projects):
    """Builds one row per epic: completed-this-week vs remaining, plus a
    KPI line for new issues created this week and net change."""
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    n_projects = len(projects)
    total_epics = sum(len(info["epics"]) for info in projects.values())
    fig_height = max(4.5, total_epics * 1.8 + n_projects * 0.9)
    fig, axes = plt.subplots(n_projects, 1, figsize=(9.5, fig_height), squeeze=False)

    colors = {"completed": "#1D9E75", "remaining": "#888780"}
    bar_h = 0.4

    label_map = strip_common_label([info["name"] for info in projects.values()])

    for ax, (project_key, info) in zip(axes[:, 0], projects.items()):
        epics = info["epics"]
        labels = [f"{e['key']}: {e['summary'][:32]}" for e in epics]
        y = list(range(len(labels)))

        max_total = 1
        for epic in epics:
            c = epic["counts"]
            total = c["to_do"] + c["in_progress"] + c["in_qa"] + c["done"]
            max_total = max(max_total, total)

        for i, epic in enumerate(epics):
            c = epic["counts"]
            total = c["to_do"] + c["in_progress"] + c["in_qa"] + c["done"]
            moved_to_done = c["moved_to_done"]
            remaining = max(total - moved_to_done, 0)

            ax.barh(i, moved_to_done, bar_h, left=0, color=colors["completed"])
            ax.barh(i, remaining, bar_h, left=moved_to_done, color=colors["remaining"])

            ax.text(
                total + max_total * 0.02, i,
                f"{moved_to_done} moved to done this week",
                va="center", fontsize=9, fontweight="bold",
            )

            net_change = moved_to_done - c["created_this_week"]
            net_label = f"+{net_change}" if net_change > 0 else str(net_change)
            kpi_line_1 = (
                f"Created this week: {c['created_this_week']}   ·   "
                f"Net change: {net_label}   ·   "
                f"Bugs: {c['bugs']}"
            )
            kpi_line_2 = (
                f"Moved to ready for test: {c['moved_to_ready_for_test']}   ·   "
                f"Moved to In QA: {c['moved_to_in_qa']}   ·   "
                f"Moved to done: {moved_to_done}"
            )
            ax.text(0, i + 0.32, kpi_line_1, va="top", fontsize=8.5, color="#5F5E5A")
            ax.text(0, i + 0.56, kpi_line_2, va="top", fontsize=8.5, color="#5F5E5A")

        ax.set_yticks(y)
        ax.set_yticklabels(labels, fontsize=10)
        ax.invert_yaxis()
        ax.set_ylim(len(epics) - 1 + 1.15, -0.7)
        ax.set_xlim(0, max_total * 1.35)
        ax.set_title(label_map[info["name"]], fontsize=12, fontweight="bold", loc="left", pad=14)
        ax.grid(axis="x", linestyle="--", alpha=0.4)
        ax.set_xlabel("Issue count", fontsize=9)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)

    legend_handles = [
        mpatches.Patch(color=colors["completed"], label="Completed this week"),
        mpatches.Patch(color=colors["remaining"], label="Remaining"),
    ]
    fig.legend(handles=legend_handles, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.06), fontsize=10, frameon=False)
    fig.tight_layout(rect=[0, 0, 1, 0.90])

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

