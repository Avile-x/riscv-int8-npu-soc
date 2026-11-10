`timescale 1ns/1ps
`include "npu_types.sv"

/* verilator lint_off UNUSEDSIGNAL */

module tb_systolic_array_4x4;
    localparam int CLK_HALF_NS = 5;
    localparam int WATCHDOG_NS = 20_000_000;
    localparam int FORMAL_CASE_COUNT = 1009;
    localparam int FORMAL_ELEMENT_COUNT = 16144;
    localparam int MAPPING_CASE_COUNT = 64;

    logic      clk;
    logic      rst;
    logic      req_valid;
    logic      req_ready;
    matrix8_t  req_a;
    matrix8_t  req_b;
    logic      rsp_valid;
    logic      rsp_ready;
    matrix32_t rsp_c;

    matrix8_t  tx_a;
    matrix8_t  tx_b;
    matrix32_t tx_exp;
    matrix32_t rsp_snapshot;
    matrix8_t  garbage_a;
    matrix8_t  garbage_b;

    logic [7:0]  formal_a [0:FORMAL_CASE_COUNT*16-1];
    logic [7:0]  formal_b [0:FORMAL_CASE_COUNT*16-1];
    logic [31:0] formal_c [0:FORMAL_CASE_COUNT*16-1];

    matrix8_t  chain_a [0:3];
    matrix8_t  chain_b [0:3];
    matrix32_t chain_exp [0:3];

    int cycles;
    int checks;
    int protocol_checks;
    int alignment_checks;
    int pe_checks;
    int capture_checks;
    int formal_cases;
    int formal_elements;
    int mapping_cases;
    int transactions;
    int reset_cases;
    int stale_cases;
    int base_cycle;
    int mon_s;
    int cfg_stall;
    int cfg_kind;
    int handshake_count;
    int tx_captures;
    int tx_injections;
    int tx_weight_rows;
    logic pe_check_active;
    logic pe_weight_bad [0:3][0:3];
    logic resp_prev_valid;
    logic resp_prev_ready;
    logic req_prev_valid;
    logic req_prev_ready;

    systolic_array_4x4 dut (
        .clk       (clk),
        .rst       (rst),
        .req_valid (req_valid),
        .req_ready (req_ready),
        .req_a     (req_a),
        .req_b     (req_b),
        .rsp_valid (rsp_valid),
        .rsp_ready (rsp_ready),
        .rsp_c     (rsp_c)
    );

    // Direct residency monitor: PE(r,c) must hold B[r][c] once LOAD completes.
    // Constant generate indices are required for Verilator hierarchical reads.
    generate
        for (genvar wr = 0; wr < 4; wr++) begin : g_wrow
            for (genvar wc = 0; wc < 4; wc++) begin : g_wcol
                always_comb begin
                    pe_weight_bad[wr][wc] =
                        pe_check_active && mon_s >= 4 &&
                        (dut.g_row[wr].g_col[wc].u_pe.weight_q !== tx_b[wr][wc]);
                end
            end
        end
    endgenerate

    initial clk = 1'b0;
    always #(CLK_HALF_NS) clk <= ~clk;

    initial begin
        $dumpfile("array.vcd");
        $dumpvars(0, tb_systolic_array_4x4);
    end

    initial begin : watchdog
        if ($test$plusargs("inject_watchdog")) #(200_000);
        else #(WATCHDOG_NS);
        $display("ARRAY_FAIL reason=watchdog_timeout cycle=%0d", cycles);
        $fatal(1, "watchdog timeout");
    end

    task automatic fail(input string reason);
        $display("ARRAY_FAIL reason=%s cycle=%0d", reason, cycles);
        $fatal(1, "array verification failed");
    endtask

    task automatic check_pe_alignment;
        logic       exp_valid;
        logic signed [31:0] exp_psum;
        int exp_act_i;
        // Independent per-PE expectations: each PE(r,c) captures the wavefront
        // carrying A[i][r] at edge t0+5+4*i+r+c and, after that edge, must hold
        // output valid=1, activation=A[i][r], resident weight=B[r][c] and
        // partial sum=sum_{k=0..r} A[i][k]*B[k][c]. Expectations are derived
        // only from tx_a/tx_b and the schedule, never from DUT state.
        if (pe_check_active && mon_s >= 4) begin
            for (int r = 0; r < 4; r++) begin
                for (int c = 0; c < 4; c++) begin
                    if (pe_weight_bad[r][c]) begin
                        $display("ARRAY_FAIL reason=pe_weight_mismatch cycle=%0d t_rel=%0d pe_r=%0d pe_c=%0d expected=%0d",
                                 cycles, mon_s, r, c, tx_b[r][c]);
                        $fatal(1, "PE weight residency violation");
                    end
                    pe_checks++;
                end
            end
        end
        if (pe_check_active) begin
            for (int r = 0; r < 4; r++) begin
                for (int c = 0; c < 4; c++) begin
                    exp_valid = 1'b0;
                    exp_psum = 32'sd0;
                    exp_act_i = 0;
                    for (int i = 0; i < 4; i++) begin
                        if (mon_s == 5 + 4*i + r + c) begin
                            exp_valid = 1'b1;
                            exp_act_i = i;
                            for (int k = 0; k <= r; k++) begin
                                exp_psum = exp_psum +
                                    $signed(tx_a[i][k]) * $signed(tx_b[k][c]);
                            end
                        end
                    end
                    if (dut.act_valid[r][c+1] !== exp_valid ||
                        dut.psum_valid[r+1][c] !== exp_valid) begin
                        $display("ARRAY_FAIL reason=pe_valid_timing cycle=%0d t_rel=%0d pe_r=%0d pe_c=%0d act_valid_out=%b psum_valid_out=%b expected=%b",
                                 cycles, mon_s, r, c,
                                 dut.act_valid[r][c+1],
                                 dut.psum_valid[r+1][c],
                                 exp_valid);
                        $fatal(1, "PE output valid timing violation");
                    end
                    pe_checks += 2;
                    if (exp_valid) begin
                        if (dut.act_data[r][c+1] !== tx_a[exp_act_i][r]) begin
                            $display("ARRAY_FAIL reason=pe_activation_mismatch cycle=%0d t_rel=%0d pe_r=%0d pe_c=%0d i=%0d got=%0d expected=%0d",
                                     cycles, mon_s, r, c, exp_act_i,
                                     dut.act_data[r][c+1],
                                     tx_a[exp_act_i][r]);
                            $fatal(1, "PE activation data violation");
                        end
                        if (dut.psum_data[r+1][c] !== exp_psum) begin
                            $display("ARRAY_FAIL reason=pe_psum_mismatch cycle=%0d t_rel=%0d pe_r=%0d pe_c=%0d i=%0d got=%0d expected=%0d",
                                     cycles, mon_s, r, c, exp_act_i,
                                     dut.psum_data[r+1][c],
                                     exp_psum);
                            $fatal(1, "PE partial sum violation");
                        end
                        pe_checks += 2;
                    end
                end
            end
        end
        for (int r = 0; r < 4; r++) begin
            for (int c = 0; c < 4; c++) begin
                if (dut.act_valid[r][c] !== dut.psum_valid[r][c]) begin
                    $display("ARRAY_FAIL reason=pe_valid_misaligned cycle=%0d pe_r=%0d pe_c=%0d act_valid=%b psum_valid=%b",
                             cycles, r, c, dut.act_valid[r][c],
                             dut.psum_valid[r][c]);
                    $fatal(1, "PE valid alignment violation");
                end
                alignment_checks++;
            end
        end
    endtask

    task automatic check_cycle_protocol;
        int active;
        if (rst) begin
            if (req_ready !== 1'b0) fail("req_ready_during_reset");
            if (rsp_valid !== 1'b0) fail("rsp_valid_during_reset");
            if (dut.state !== IDLE) fail("state_not_idle_during_reset");
        end else begin
            if (dut.state == IDLE) begin
                if (req_ready !== 1'b1) fail("req_ready_low_in_idle");
                active = 0;
                for (int r = 0; r < 4; r++) begin
                    for (int c = 0; c < 4; c++) begin
                        if (dut.act_valid[r][c] === 1'b1) active++;
                        if (dut.psum_valid[r][c] === 1'b1) active++;
                    end
                end
                if (active != 0) fail("valid_active_in_idle");
            end else if (req_ready !== 1'b0) begin
                fail("req_ready_early");
            end
            if (rsp_valid === 1'b1 && dut.state !== HOLD_RESULT) begin
                fail("rsp_valid_outside_hold");
            end
            check_pe_alignment();
        end
        protocol_checks++;
    endtask

    task automatic tick;
        @(posedge clk);
        #1;
        cycles++;
        check_cycle_protocol();
    endtask

    task automatic check_capture;
        int bottom_valid;
        bottom_valid = 0;
        for (int c = 0; c < 4; c++) begin
            if (dut.psum_valid[4][c] === 1'b1) bottom_valid++;
        end
        if (mon_s >= 8 && mon_s <= 23) begin
            if (bottom_valid != 1) fail("bottom_valid_count");
            for (int c = 0; c < 4; c++) begin
                if (dut.psum_valid[4][c] !== ((c == (mon_s % 4)) ? 1'b1 : 1'b0))
                    fail("bottom_valid_column");
            end
            if (dut.capture_hit !== 1'b1) fail("capture_missing");
            if (dut.result_idx !== 4'(mon_s - 8)) fail("capture_index");
            tx_captures++;
            capture_checks++;
        end else begin
            if (bottom_valid != 0) fail("bottom_valid_outside_window");
            if (dut.capture_hit !== 1'b0) fail("capture_outside_window");
        end
        if (mon_s == 23 && dut.capture_last !== 1'b1) fail("capture_last_missing");
        if (mon_s != 23 && dut.capture_last !== 1'b0) fail("capture_last_early");
        if (mon_s == 24 && dut.result_idx !== 4'd15) fail("capture_index_final");
    endtask

    task automatic load_formal_case(input int index);
        int base;
        base = index * 16;
        for (int r = 0; r < 4; r++) begin
            for (int c = 0; c < 4; c++) begin
                tx_a[r][c] = formal_a[base + r*4 + c];
                tx_b[r][c] = formal_b[base + r*4 + c];
                tx_exp[r][c] = formal_c[base + r*4 + c];
            end
        end
    endtask

    task automatic build_mapping_case(input int i, input int k, input int j);
        for (int r = 0; r < 4; r++) begin
            for (int c = 0; c < 4; c++) begin
                tx_a[r][c] = 8'sd0;
                tx_b[r][c] = 8'sd0;
                tx_exp[r][c] = 32'sd0;
            end
        end
        tx_a[i][k] = 8'sd2;
        tx_b[k][j] = 8'sd3;
        tx_exp[i][j] = 32'sd6;
    endtask

    task automatic build_special_case(input int which);
        for (int r = 0; r < 4; r++) begin
            for (int c = 0; c < 4; c++) begin
                tx_a[r][c] = 8'sd0;
                tx_b[r][c] = 8'sd0;
                tx_exp[r][c] = 32'sd0;
            end
        end
        if (which == 0) begin
            for (int r = 0; r < 4; r++) begin
                for (int c = 0; c < 4; c++) begin
                    tx_a[r][c] = 8'sd1;
                    tx_b[r][c] = 8'sd1;
                    tx_exp[r][c] = 32'sd4;
                end
            end
        end else if (which == 1) begin
            for (int r = 0; r < 4; r++) begin
                for (int c = 0; c < 4; c++) begin
                    tx_a[r][c] = (r == c) ? int8_t'(r + 1) : 8'sd0;
                    tx_b[r][c] = (r == c) ? int8_t'(2*r + 5) : 8'sd0;
                    tx_exp[r][c] = (r == c) ? 32'((r + 1) * (2*r + 5)) : 32'sd0;
                end
            end
        end else if (which == 2) begin
            for (int r = 0; r < 4; r++) begin
                for (int c = 0; c < 4; c++) begin
                    tx_a[r][c] = -8'sd2;
                    tx_b[r][c] = 8'sd3;
                    tx_exp[r][c] = -32'sd24;
                end
            end
        end else if (which == 3) begin
            tx_a[0][0] = 8'sd5;
            tx_b[0][2] = 8'sd7;
            tx_exp[0][2] = 32'sd35;
        end else begin
            for (int r = 0; r < 4; r++) begin
                for (int c = 0; c < 4; c++) begin
                    tx_a[r][c] = int8_t'((r + 1) * (c + 1));
                    tx_b[r][c] = (r == c) ? 8'sd1 : 8'sd0;
                    tx_exp[r][c] = int32_t'(tx_a[r][c]);
                end
            end
        end
    endtask

    task automatic compare_response(input int kind);
        for (int r = 0; r < 4; r++) begin
            for (int c = 0; c < 4; c++) begin
                if (rsp_c[r][c] !== tx_exp[r][c]) begin
                    $display("ARRAY_FAIL reason=result_mismatch cycle=%0d field=c[%0d][%0d] got=%08h expected=%08h",
                             cycles, r, c, rsp_c[r][c], tx_exp[r][c]);
                    $fatal(1, "result mismatch");
                end
                checks++;
            end
        end
        if (kind == 0) begin
            formal_cases++;
            formal_elements += 16;
        end else if (kind == 1) begin
            mapping_cases++;
        end
    endtask

    task automatic check_transaction;
        int inject_i;
        int inject_k;
        if (mon_s <= 3) begin
            if (dut.state !== LOAD_WEIGHTS) fail("state_load_expected");
            if (dut.load_row !== 2'(mon_s)) fail("load_row_index");
            if (dut.weight_we !== (4'b0001 << mon_s)) fail("weight_onehot");
            for (int c = 0; c < 4; c++) begin
                if (dut.b_buf[mon_s][c] !== tx_b[mon_s][c])
                    fail("weight_cache_mismatch");
            end
            tx_weight_rows++;
            checks++;
        end else begin
            if (dut.weight_we !== 4'b0000) fail("weight_we_after_load");
        end

        if (mon_s >= 4 && mon_s <= 19) begin
            if (dut.state !== COMPUTE) fail("state_compute_expected");
            if (dut.issue_idx !== 4'(mon_s - 4)) fail("issue_index");
            inject_k = (mon_s - 4) % 4;
            inject_i = (mon_s - 4) / 4;
            for (int r = 0; r < 4; r++) begin
                if (dut.inject_act_valid[r] !== ((r == inject_k) ? 1'b1 : 1'b0))
                    fail("inject_act_select");
                if (dut.inject_psum_valid[r] !== ((r == inject_k) ? 1'b1 : 1'b0))
                    fail("inject_psum_select");
            end
            if (dut.inject_act_data[inject_k] !== tx_a[inject_i][inject_k])
                fail("inject_act_value");
            if (dut.inject_psum_data[inject_k] !== 32'sd0)
                fail("inject_psum_seed");
            tx_injections++;
            checks += 4;
        end else begin
            for (int r = 0; r < 4; r++) begin
                if (dut.inject_act_valid[r] !== 1'b0)
                    fail("inject_outside_compute");
                if (dut.inject_psum_valid[r] !== 1'b0)
                    fail("inject_psum_outside_compute");
            end
        end

        if (mon_s >= 20 && mon_s <= 23) begin
            if (dut.state !== DRAIN) fail("state_drain_expected");
        end
        if (mon_s >= 24 && mon_s < 25 + cfg_stall) begin
            if (dut.state !== HOLD_RESULT) fail("state_hold_expected");
        end
        if (mon_s == 25 + cfg_stall) begin
            if (dut.state !== IDLE) fail("state_idle_after_handshake");
            if (req_ready !== 1'b1) fail("req_ready_after_handshake");
        end
        if (mon_s < 25 + cfg_stall && req_ready !== 1'b0) fail("req_ready_early_busy");

        if (mon_s == 4 || mon_s == 20) begin
            for (int r = 0; r < 4; r++) begin
                for (int c = 0; c < 4; c++) begin
                    if (dut.a_buf[r][c] !== tx_a[r][c]) fail("request_cache_a");
                    if (dut.b_buf[r][c] !== tx_b[r][c]) fail("request_cache_b");
                    checks += 2;
                end
            end
        end

        if (mon_s < 24) begin
            if (rsp_valid !== 1'b0) fail("rsp_valid_early");
            checks++;
        end else if (mon_s == 24) begin
            if (rsp_valid !== 1'b1) fail("rsp_valid_missing");
            compare_response(cfg_kind);
            for (int r = 0; r < 4; r++) begin
                for (int c = 0; c < 4; c++) begin
                    rsp_snapshot[r][c] = rsp_c[r][c];
                end
            end
        end else if (mon_s < 25 + cfg_stall) begin
            if (rsp_valid !== 1'b1) fail("rsp_valid_dropped_in_stall");
            for (int r = 0; r < 4; r++) begin
                for (int c = 0; c < 4; c++) begin
                    if (rsp_c[r][c] !== rsp_snapshot[r][c]) begin
                        $display("ARRAY_FAIL reason=response_unstable cycle=%0d field=c[%0d][%0d] got=%08h expected=%08h",
                                 cycles, r, c, rsp_c[r][c], rsp_snapshot[r][c]);
                        $fatal(1, "response changed while stalled");
                    end
                    checks++;
                end
            end
        end else begin
            if (rsp_valid !== 1'b0) fail("rsp_valid_not_cleared");
            checks++;
        end
    endtask

    task automatic run_normal(input int stall, input int kind, input logic change_inputs,
                              input logic inject_pe_timing = 1'b0,
                              input logic inject_pe_weight = 1'b0);
        int s;
        cfg_stall = stall;
        cfg_kind = kind;
        @(negedge clk);
        for (int r = 0; r < 4; r++) begin
            for (int c = 0; c < 4; c++) begin
                req_a[r][c] = tx_a[r][c];
                req_b[r][c] = tx_b[r][c];
            end
        end
        req_valid = 1'b1;
        rsp_ready = 1'b1;
        req_prev_valid = req_valid;
        req_prev_ready = req_ready;
        tick();
        if (!(req_prev_valid === 1'b1 && req_prev_ready === 1'b1))
            fail("request_not_accepted");
        base_cycle = cycles;
        transactions++;
        handshake_count = 0;
        tx_captures = 0;
        tx_injections = 0;
        tx_weight_rows = 0;
        mon_s = 0;
        pe_check_active = 1'b1;
        check_transaction();
        check_capture();
        for (s = 1; s <= 25 + stall; s++) begin
            @(negedge clk);
            req_valid = 1'b0;
            if (stall > 0 && s >= 24 && s < 25 + stall) rsp_ready = 1'b0;
            else rsp_ready = 1'b1;
            if (change_inputs) begin
                for (int r = 0; r < 4; r++) begin
                    for (int c = 0; c < 4; c++) begin
                        req_a[r][c] = garbage_a[r][c];
                        req_b[r][c] = garbage_b[r][c];
                    end
                end
            end
`ifndef VERILATOR
            if (inject_pe_timing && s == 1) begin
                force dut.g_row[0].g_col[0].u_pe.act_valid_out = 1'b1;
                force dut.g_row[0].g_col[0].u_pe.psum_valid_out = 1'b1;
            end
            if (inject_pe_weight && s == 4) begin
                force dut.g_row[0].g_col[0].u_pe.weight_q = 8'sd2;
            end
`endif
            mon_s = s;
            resp_prev_valid = rsp_valid;
            resp_prev_ready = rsp_ready;
            tick();
            if (resp_prev_valid === 1'b1 && resp_prev_ready === 1'b1) begin
                handshake_count++;
                if (cycles !== base_cycle + 25 + stall)
                    fail("response_handshake_timing");
            end
            check_transaction();
            check_capture();
        end
        if (handshake_count != 1) fail("response_not_handshaken");
        if (tx_captures != 16) fail("capture_count");
        if (tx_injections != 16) fail("injection_count");
        if (tx_weight_rows != 4) fail("weight_row_count");
        req_valid = 1'b0;
        rsp_ready = 1'b1;
        pe_check_active = 1'b0;
    endtask

    task automatic run_reset_stage(input int stage);
        int s;
        int target;
        @(negedge clk);
        for (int r = 0; r < 4; r++) begin
            for (int c = 0; c < 4; c++) begin
                req_a[r][c] = tx_a[r][c];
                req_b[r][c] = tx_b[r][c];
            end
        end
        req_valid = 1'b1;
        rsp_ready = 1'b1;
        req_prev_valid = req_valid;
        req_prev_ready = req_ready;
        tick();
        if (!(req_prev_valid === 1'b1 && req_prev_ready === 1'b1))
            fail("reset_request_not_accepted");
        base_cycle = cycles;
        transactions++;
        if (stage == 1) target = 1;
        else if (stage == 2) target = 10;
        else if (stage == 3) target = 21;
        else target = 24;
        for (s = 1; s < target; s++) begin
            @(negedge clk);
            req_valid = 1'b0;
            rsp_ready = 1'b1;
            tick();
            if (rsp_valid !== 1'b0) fail("unexpected_rsp_before_reset");
        end
        if (stage >= 4) begin
            @(negedge clk);
            req_valid = 1'b0;
            rsp_ready = (stage == 5) ? 1'b1 : 1'b0;
            tick();
            if (rsp_valid !== 1'b1) fail("hold_response_missing");
            if (dut.state !== HOLD_RESULT) fail("hold_state_missing");
        end
        @(negedge clk);
        rst = 1'b1;
        req_valid = 1'b0;
        rsp_ready = (stage == 5) ? 1'b1 : 1'b0;
        #1;
        if (req_ready !== 1'b0) fail("req_ready_pre_posedge_reset");
        if (rsp_valid !== 1'b0) fail("rsp_valid_pre_posedge_reset");
        tick();
        if (rsp_valid !== 1'b0) fail("residual_rsp_after_reset");
        if (req_ready !== 1'b0) fail("req_ready_after_reset_assert");
        if (dut.state !== IDLE) fail("state_after_reset_assert");
        @(negedge clk);
        rst = 1'b0;
        req_valid = 1'b0;
        rsp_ready = 1'b1;
        tick();
        if (rsp_valid !== 1'b0) fail("residual_rsp_after_reset_release");
        if (req_ready !== 1'b1) fail("req_ready_after_reset_release");
        if (dut.state !== IDLE) fail("state_after_reset_release");
        reset_cases++;
        if (stage == 4) begin
            // Response was canceled by reset while in HOLD; a fresh task must
            // still be accepted and complete normally.
            run_normal(0, 2, 1'b0);
        end
    endtask

    task automatic run_reset_request_edge;
        @(negedge clk);
        rst = 1'b1;
        for (int r = 0; r < 4; r++) begin
            for (int c = 0; c < 4; c++) begin
                req_a[r][c] = tx_a[r][c];
                req_b[r][c] = tx_b[r][c];
            end
        end
        req_valid = 1'b1;
        rsp_ready = 1'b1;
        #1;
        req_prev_valid = req_valid;
        req_prev_ready = req_ready;
        tick();
        if (req_prev_valid !== 1'b1) fail("reset_request_valid_lost");
        if (req_prev_ready !== 1'b0) fail("req_ready_high_reset_request");
        if (rsp_valid !== 1'b0) fail("rsp_on_reset_request");
        if (dut.state !== IDLE) fail("state_reset_request");
        @(negedge clk);
        rst = 1'b0;
        req_valid = 1'b0;
        rsp_ready = 1'b1;
        tick();
        if (req_ready !== 1'b1) fail("req_ready_after_reset_request");
        if (rsp_valid !== 1'b0) fail("residual_rsp_reset_request");
        reset_cases++;
    endtask

    task automatic run_chain;
        int k;
        int s;
        int prev_base;
        @(negedge clk);
        req_valid = 1'b1;
        for (int r = 0; r < 4; r++) begin
            for (int c = 0; c < 4; c++) begin
                req_a[r][c] = chain_a[0][r][c];
                req_b[r][c] = chain_b[0][r][c];
            end
        end
        rsp_ready = 1'b1;
        pe_check_active = 1'b1;
        for (k = 0; k < 4; k++) begin
            req_prev_valid = req_valid;
            req_prev_ready = req_ready;
            tick();
            if (!(req_prev_valid === 1'b1 && req_prev_ready === 1'b1))
                fail("chain_request_not_accepted");
            if (k > 0 && cycles !== prev_base + 26) fail("next_accept_timing");
            base_cycle = cycles;
            prev_base = cycles;
            transactions++;
            handshake_count = 0;
            tx_captures = 0;
            tx_injections = 0;
            tx_weight_rows = 0;
            cfg_stall = 0;
            cfg_kind = 2;
            for (int r = 0; r < 4; r++) begin
                for (int c = 0; c < 4; c++) begin
                    tx_a[r][c] = chain_a[k][r][c];
                    tx_b[r][c] = chain_b[k][r][c];
                    tx_exp[r][c] = chain_exp[k][r][c];
                end
            end
            mon_s = 0;
            check_transaction();
            check_capture();
            for (s = 1; s <= 25; s++) begin
                @(negedge clk);
                if (s == 1) begin
                    if (k < 3) begin
                        for (int r = 0; r < 4; r++) begin
                            for (int c = 0; c < 4; c++) begin
                                req_a[r][c] = chain_a[k+1][r][c];
                                req_b[r][c] = chain_b[k+1][r][c];
                            end
                        end
                        req_valid = 1'b1;
                    end else begin
                        req_valid = 1'b0;
                    end
                    rsp_ready = 1'b1;
                end
                mon_s = s;
                resp_prev_valid = rsp_valid;
                resp_prev_ready = rsp_ready;
                tick();
                if (resp_prev_valid === 1'b1 && resp_prev_ready === 1'b1) begin
                    handshake_count++;
                    if (cycles !== base_cycle + 25) fail("chain_response_timing");
                end
                check_transaction();
                check_capture();
            end
            if (handshake_count != 1) fail("chain_handshake_count");
            if (tx_captures != 16) fail("chain_capture_count");
            if (k > 0) stale_cases++;
        end
        req_valid = 1'b0;
        rsp_ready = 1'b1;
        pe_check_active = 1'b0;
    endtask

    initial begin : main
        int ci;
        int mi;
        int mk;
        int mj;
        rst = 1'b1;
        req_valid = 1'b0;
        rsp_ready = 1'b0;
        for (int r = 0; r < 4; r++) begin
            for (int c = 0; c < 4; c++) begin
                req_a[r][c] = 8'sd0;
                req_b[r][c] = 8'sd0;
                tx_a[r][c] = 8'sd0;
                tx_b[r][c] = 8'sd0;
                tx_exp[r][c] = 32'sd0;
                rsp_snapshot[r][c] = 32'sd0;
                garbage_a[r][c] = int8_t'(100 + r*4 + c);
                garbage_b[r][c] = int8_t'(-120 + r*4 + c);
                chain_a[0][r][c] = 8'sd1;
                chain_b[0][r][c] = 8'sd1;
                chain_exp[0][r][c] = 32'sd4;
                chain_a[1][r][c] = (r == c) ? int8_t'(r + 2) : 8'sd0;
                chain_b[1][r][c] = (r == c) ? 8'sd1 : 8'sd0;
                chain_exp[1][r][c] = int32_t'(chain_a[1][r][c]);
                chain_a[2][r][c] = -8'sd2;
                chain_b[2][r][c] = 8'sd3;
                chain_exp[2][r][c] = -32'sd24;
                chain_a[3][r][c] = 8'sd0;
                chain_b[3][r][c] = 8'sd0;
                chain_exp[3][r][c] = 32'sd0;
            end
        end
        chain_a[3][0][0] = 8'sd5;
        chain_b[3][0][2] = 8'sd7;
        chain_exp[3][0][2] = 32'sd35;

        cycles = 0;
        checks = 0;
        protocol_checks = 0;
        alignment_checks = 0;
        pe_checks = 0;
        capture_checks = 0;
        formal_cases = 0;
        formal_elements = 0;
        mapping_cases = 0;
        transactions = 0;
        reset_cases = 0;
        stale_cases = 0;
        base_cycle = 0;
        mon_s = 0;
        cfg_stall = 0;
        cfg_kind = 0;
        handshake_count = 0;
        tx_captures = 0;
        tx_injections = 0;
        tx_weight_rows = 0;
        pe_check_active = 1'b0;

        tick();
        tick();
        rst = 1'b0;
        tick();
        if (req_ready !== 1'b1) fail("initial_req_ready");
        if (dut.state !== IDLE) fail("initial_state");
        if (rsp_valid !== 1'b0) fail("initial_rsp_valid");

        if ($test$plusargs("inject_watchdog")) begin
            $display("ARRAY_INFO reason=watchdog_test_started");
            forever @(posedge clk);
        end

        if ($test$plusargs("inject_pe_timing")) begin
            build_special_case(0);
            run_normal(0, 2, 1'b0, 1'b1, 1'b0);
            $display("ARRAY_FAIL reason=inject_pe_timing_not_detected cycle=%0d", cycles);
            $fatal(1, "PE timing fault not detected");
        end

        if ($test$plusargs("inject_pe_weight")) begin
            build_special_case(0);
            run_normal(0, 2, 1'b0, 1'b0, 1'b1);
            $display("ARRAY_FAIL reason=inject_pe_weight_not_detected cycle=%0d", cycles);
            $fatal(1, "PE weight fault not detected");
        end

        if ($test$plusargs("inject_x") ||
            $test$plusargs("inject_z") ||
            $test$plusargs("inject_mismatch")) begin
            build_special_case(0);
`ifndef VERILATOR
            if ($test$plusargs("inject_x"))
                force dut.rsp_c[0][0] = 32'hxxxxxxxx;
            else if ($test$plusargs("inject_z"))
                force dut.rsp_c[1][1] = 32'hzzzzzzzz;
            else
                force dut.rsp_c[2][3] = 32'h01234567;
`endif
            run_normal(0, 2, 1'b0);
            $display("ARRAY_FAIL reason=inject_not_detected cycle=%0d", cycles);
            $fatal(1, "injected fault not detected");
        end

        $readmemh("formal_a.hex", formal_a);
        $readmemh("formal_b.hex", formal_b);
        $readmemh("formal_c.hex", formal_c);

        for (ci = 0; ci < FORMAL_CASE_COUNT; ci++) begin
            load_formal_case(ci);
            run_normal(0, 0, 1'b0);
        end

        for (mi = 0; mi < 4; mi++) begin
            for (mk = 0; mk < 4; mk++) begin
                for (mj = 0; mj < 4; mj++) begin
                    build_mapping_case(mi, mk, mj);
                    run_normal(0, 1, 1'b0);
                end
            end
        end

        build_special_case(0);
        run_normal(1, 2, 1'b0);
        build_special_case(1);
        run_normal(4, 2, 1'b0);
        build_special_case(2);
        run_normal(0, 2, 1'b1);
        run_chain();

        build_special_case(1);
        run_reset_stage(1);
        build_special_case(0);
        run_normal(0, 2, 1'b0);
        build_special_case(1);
        run_reset_stage(2);
        build_special_case(2);
        run_normal(0, 2, 1'b0);
        build_special_case(1);
        run_reset_stage(3);
        build_special_case(3);
        run_normal(0, 2, 1'b0);
        build_special_case(1);
        run_reset_stage(4);
        build_special_case(4);
        run_normal(0, 2, 1'b0);
        build_special_case(1);
        run_reset_stage(5);
        build_special_case(0);
        run_normal(0, 2, 1'b0);
        build_special_case(1);
        run_reset_request_edge();
        build_special_case(2);
        run_normal(0, 2, 1'b0);

        if (dut.state !== IDLE) fail("final_not_idle");
        if (req_ready !== 1'b1) fail("final_req_ready");
        if (rsp_valid !== 1'b0) fail("final_rsp_valid");
        if (formal_cases != FORMAL_CASE_COUNT) fail("formal_case_count");
        if (formal_elements != FORMAL_ELEMENT_COUNT) fail("formal_element_count");
        if (mapping_cases != MAPPING_CASE_COUNT) fail("mapping_case_count");
        if (reset_cases != 6) fail("reset_case_count");
        if (stale_cases != 3) fail("stale_case_count");

        $display("ARRAY_SUMMARY formal_cases=%0d formal_elements=%0d mapping_cases=%0d transactions=%0d checks=%0d protocol_checks=%0d alignment_checks=%0d pe_checks=%0d capture_checks=%0d reset_cases=%0d stale_cases=%0d",
                 formal_cases, formal_elements, mapping_cases, transactions, checks,
                 protocol_checks, alignment_checks, pe_checks, capture_checks,
                 reset_cases, stale_cases);
        $display("ARRAY_PASS checks=%0d formal_cases=%0d formal_elements=%0d mapping_cases=%0d",
                 checks, formal_cases, formal_elements, mapping_cases);
        $finish;
    end
endmodule
