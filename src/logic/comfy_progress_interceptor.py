# -*- coding: utf-8 -*-

class ComfyProgressInterceptor:
    """
    ComfyUI 进度拦截器：
    解决 ComfyUI 多节点或内部切片循环导致的进度条“回跳”问题。
    使用高水位锁和“芝诺逼近法”合成单调递增的 overall_progress。
    """
    def __init__(self, callback, total_nodes):
        self.callback = callback
        self.total = total_nodes
        self.executed_nodes = set()
        self.max_percent = 0.0

    def __call__(self, data):
        msg_type = data.get("type", "")
        
        should_update_progress = False
        current_sub = 0
        
        if msg_type == "executing":
            node_id = data.get("data", {}).get("node")
            if node_id:
                self.executed_nodes.add(node_id)
                should_update_progress = True
        elif msg_type == "execution_cached":
            nodes = data.get("data", {}).get("nodes", [])
            if nodes:
                for node_id in nodes:
                    self.executed_nodes.add(node_id)
                should_update_progress = True
                
        # 透传原始事件以保证后向兼容性
        self.callback(data)
        
        if msg_type == "progress":
            val = data.get("data", {}).get("value", 0)
            max_val = data.get("data", {}).get("max", 1)
            current_sub = val / max_val if max_val > 0 else 0
            should_update_progress = True
            
        if should_update_progress:
            completed = max(0, len(self.executed_nodes) - 1)
            raw_global = (completed + current_sub) / self.total if self.total > 0 else 0
            raw_percent = raw_global * 100
            
            if raw_percent >= self.max_percent:
                self.max_percent = raw_percent
            else:
                remaining = 99.0 - self.max_percent
                fake_inc = 0.5 if remaining > 10 else (0.1 if remaining > 2 else 0)
                self.max_percent += fake_inc
                
            final_pct = min(99, int(self.max_percent))
            
            self.callback({
                "type": "overall_progress",
                "data": {
                    "percent": final_pct,
                    "message": f"执行中 ({final_pct}%)"
                }
            })
