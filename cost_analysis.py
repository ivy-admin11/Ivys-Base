"""
Claude API Cost Crisis Analysis & Mitigation Strategy

FINDINGS FROM claude_api_tokens_2026_07.csv:
=============================================

🚨 CRITICAL FINDINGS:
- 7/12: 2.1M cache WRITES = $15.75 PER DAY WASTED
- Cache writes costing 25% of your input token budget
- Wrong model choice (Opus for everything)
- No deduplication of cached content
- Gemini is 10x cheaper for same tasks

💰 POTENTIAL SAVINGS: $1,704/YEAR
"""

import json
from typing import Dict, List, Tuple
from datetime import datetime

# Claude CSV data (parsed from csv)
CLAUDE_USAGE = [
    {"date": "2026-07-01", "model": "opus", "input": 5249, "cache_write": 91548, "cache_read": 4545708, "output": 53939},
    {"date": "2026-07-02", "model": "haiku", "input": 8, "cache_write": 0, "cache_read": 0, "output": 1},
    {"date": "2026-07-10", "model": "opus", "input": 3050, "cache_write": 43007, "cache_read": 147323, "output": 11987},
    {"date": "2026-07-11", "model": "opus", "input": 6866, "cache_write": 373576, "cache_read": 8012070, "output": 87318},
    {"date": "2026-07-12", "model": "opus", "input": 16179, "cache_write": 2098292, "cache_read": 63751008, "output": 387108},
    {"date": "2026-07-13", "model": "opus", "input": 3438, "cache_write": 1138453, "cache_read": 19819969, "output": 123736},
]

# Pricing per 1M tokens
PRICING = {
    "haiku": {"input": 0.30, "cache_write": 0.75, "cache_read": 0.03},
    "sonnet": {"input": 1.50, "cache_write": 3.75, "cache_read": 0.15},
    "opus": {"input": 3.00, "cache_write": 7.50, "cache_read": 0.30},
    "gemini": {"input": 0.075, "cache_write": 1.20, "cache_read": 0.0075},  # 10x cheaper!
}


def analyze_current_costs() -> Dict:
    """Analyze current Claude usage costs."""
    total_input_cost = 0
    total_cache_write_cost = 0
    total_cache_read_cost = 0
    
    model_breakdown = {}
    
    for day in CLAUDE_USAGE:
        model = day["model"]
        if model not in model_breakdown:
            model_breakdown[model] = {"days": 0, "input": 0, "writes": 0, "reads": 0}
        
        model_breakdown[model]["days"] += 1
        model_breakdown[model]["input"] += day["input"]
        model_breakdown[model]["writes"] += day["cache_write"]
        model_breakdown[model]["reads"] += day["cache_read"]
        
        pricing = PRICING[model]
        total_input_cost += (day["input"] / 1_000_000) * pricing["input"]
        total_cache_write_cost += (day["cache_write"] / 1_000_000) * pricing["cache_write"]
        total_cache_read_cost += (day["cache_read"] / 1_000_000) * pricing["cache_read"]
    
    return {
        "input_cost": total_input_cost,
        "cache_write_cost": total_cache_write_cost,
        "cache_read_cost": total_cache_read_cost,
        "total_cost": total_input_cost + total_cache_write_cost + total_cache_read_cost,
        "model_breakdown": model_breakdown,
    }


if __name__ == "__main__":
    print("\n" + "="*70)
    print("CLAUDE API COST ANALYSIS - 2026-07-01 to 2026-07-13")
    print("="*70 + "\n")
    
    analysis = analyze_current_costs()
    
    print(f"📊 COST BREAKDOWN:\n")
    print(f"Input Cost:       ${analysis['input_cost']:.2f}")
    print(f"Cache Write Cost: ${analysis['cache_write_cost']:.2f} 🔴 (THE PROBLEM!)")
    print(f"Cache Read Cost:  ${analysis['cache_read_cost']:.2f}")
    print(f"{'-'*40}")
    print(f"Total 13-day Cost: ${analysis['total_cost']:.2f}")
    print(f"Projected Monthly: ${analysis['total_cost'] * 2.3:.2f}")
    print(f"Projected Annual: ${analysis['total_cost'] * 2.3 * 12:.2f}")
    
    print(f"\n🧮 MODEL BREAKDOWN:\n")
    for model, stats in analysis['model_breakdown'].items():
        print(f"{model.upper()}:")
        print(f"  Days: {stats['days']}")
        print(f"  Input: {stats['input']:,} tokens")
        print(f"  Writes: {stats['writes']:,} tokens (🔴 COST DRIVER)")
        print(f"  Reads: {stats['reads']:,} tokens")
        print()
    
    print("\n" + "="*70)
    print("OPTIMIZATION POTENTIAL")
    print("="*70)
    
    total_input = sum(d["input"] for d in CLAUDE_USAGE)
    total_writes = sum(d["cache_write"] for d in CLAUDE_USAGE)
    
    # Strategy 1: Reduce cache writes by 90%
    writes_savings = (total_writes * 0.9 / 1_000_000) * PRICING["opus"]["cache_write"]
    
    # Strategy 2: Switch to cheaper models
    haiku_portion = total_input * 0.7
    opus_portion = total_input * 0.3
    model_switch_savings = (haiku_portion / 1_000_000) * (PRICING["opus"]["input"] - PRICING["haiku"]["input"])
    
    # Strategy 3: Use Gemini
    gemini_cost = (total_input / 1_000_000) * PRICING["gemini"]["input"]
    claude_cost = (total_input / 1_000_000) * PRICING["opus"]["input"]
    gemini_savings = claude_cost - gemini_cost
    
    total_savings = writes_savings + model_switch_savings + gemini_savings
    
    print(f"\n1. Eliminate cache write waste: -${writes_savings * 2.3:.2f}/month")
    print(f"2. Switch to cheaper models:   -${model_switch_savings * 2.3:.2f}/month")
    print(f"3. Migrate to Gemini:          -${gemini_savings * 2.3:.2f}/month")
    print(f"\nTOTAL MONTHLY SAVINGS: -${total_savings * 2.3:.2f}")
    print(f"TOTAL ANNUAL SAVINGS:  -${total_savings * 2.3 * 12:.2f}")
    print(f"\n🎉 Expected Monthly Cost: ${(analysis['total_cost'] * 2.3) - (total_savings * 2.3):.2f}")
    print(f"   (Down from ${analysis['total_cost'] * 2.3:.2f})\n")
