"use client";

import React from "react";
import {
  type Task,
  AGENT_LABELS,
  PRIORITY_STYLES,
  STATUS_COLUMNS,
} from "@/lib/task-config";

interface TaskCardProps {
  task: Task;
  columnKey: string;
  onStatusChange: (id: string, status: string) => void;
  onDelete: (id: string) => void;
  compact?: boolean;
}

export default function TaskCard({
  task,
  columnKey,
  onStatusChange,
  onDelete,
  compact = false,
}: TaskCardProps) {
  const agent = AGENT_LABELS[task.agent] || { name: task.agent, color: "#5F5E5A" };
  const priority = PRIORITY_STYLES[task.priority] || PRIORITY_STYLES.medium;
  const colIdx = STATUS_COLUMNS.findIndex((s) => s.key === columnKey);

  return (
    <div
      className={`bg-white rounded-lg border border-[#E0DED8] shadow-sm hover:shadow transition group ${
        compact ? "p-2" : "p-3"
      }`}
    >
      <p
        className={`text-[#2C2C2A] ${
          compact
            ? "text-xs mb-1.5 truncate"
            : "text-sm mb-2 leading-relaxed"
        }`}
        title={compact ? task.task : undefined}
      >
        {task.task}
      </p>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span
            className="text-[10px] font-medium px-1.5 py-0.5 rounded"
            style={{ backgroundColor: agent.color + "15", color: agent.color }}
          >
            {agent.name}
          </span>
          <span
            className="text-[10px] font-medium px-1.5 py-0.5 rounded"
            style={{ backgroundColor: priority.bg, color: priority.color }}
          >
            {priority.label}
          </span>
        </div>
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition">
          {colIdx > 0 && (
            <button
              onClick={() => onStatusChange(task.id, STATUS_COLUMNS[colIdx - 1].key)}
              className="text-[#B0AFA8] hover:text-[#534AB7] transition"
              title="Move left"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
              </svg>
            </button>
          )}
          {colIdx < STATUS_COLUMNS.length - 1 && (
            <button
              onClick={() => onStatusChange(task.id, STATUS_COLUMNS[colIdx + 1].key)}
              className="text-[#B0AFA8] hover:text-[#534AB7] transition"
              title="Move right"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
              </svg>
            </button>
          )}
          <button
            onClick={() => onDelete(task.id)}
            className="text-[#B0AFA8] hover:text-[#D85A30] transition"
            title="Delete"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}
