"use client";

import React from "react";
import { type Task, STATUS_COLUMNS } from "@/lib/task-config";
import TaskCard from "./TaskCard";

interface KanbanBoardProps {
  tasks: Task[];
  onStatusChange: (id: string, status: string) => void;
  onDelete: (id: string) => void;
  compact?: boolean;
}

export default function KanbanBoard({
  tasks,
  onStatusChange,
  onDelete,
  compact = false,
}: KanbanBoardProps) {
  return (
    <div
      className={`grid gap-3 ${
        compact
          ? "grid-cols-2 lg:grid-cols-4"
          : "grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4"
      }`}
    >
      {STATUS_COLUMNS.map((col) => {
        const colTasks = tasks.filter((t) => t.status === col.key);
        return (
          <div key={col.key} className="flex flex-col min-w-0">
            <div className={`flex items-center gap-1.5 px-1 ${compact ? "mb-1.5" : "mb-3"}`}>
              <span
                className={`rounded-full ${compact ? "w-2 h-2" : "w-2.5 h-2.5"}`}
                style={{ backgroundColor: col.color }}
              />
              <span
                className={`font-semibold text-[#2C2C2A] ${compact ? "text-[11px]" : "text-sm"}`}
              >
                {col.label}
              </span>
              <span
                className={`text-[#5F5E5A] bg-[#F8F8F6] rounded-full ${
                  compact ? "text-[10px] px-1.5 py-px" : "text-xs px-2 py-0.5"
                }`}
              >
                {colTasks.length}
              </span>
            </div>
            <div
              className={`flex-1 space-y-1.5 bg-[#F8F8F6]/50 rounded-xl border border-[#E0DED8]/50 ${
                compact ? "p-1.5 min-h-[60px]" : "p-2 min-h-[100px] space-y-2"
              }`}
            >
              {colTasks.length === 0 && (
                <p
                  className={`text-[#B0AFA8] text-center ${
                    compact ? "text-[10px] py-3" : "text-xs py-6"
                  }`}
                >
                  No tasks
                </p>
              )}
              {colTasks.map((task) => (
                <TaskCard
                  key={task.id}
                  task={task}
                  columnKey={col.key}
                  onStatusChange={onStatusChange}
                  onDelete={onDelete}
                  compact={compact}
                />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
