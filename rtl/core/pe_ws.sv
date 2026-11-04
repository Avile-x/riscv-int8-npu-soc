`timescale 1ns/1ps
`include "npu_types.sv"

module pe_ws (
    input  logic   clk,
    input  logic   rst,
    input  logic   pipe_en,
    input  logic   weight_we,
    input  int8_t  weight_in,
    input  int8_t  act_in,
    input  logic   act_valid_in,
    input  int32_t psum_in,
    input  logic   psum_valid_in,
    output int8_t  act_out,
    output logic   act_valid_out,
    output int32_t psum_out,
    output logic   psum_valid_out
);
    int8_t weight_q;
    wire signed [15:0] product;
    wire signed [31:0] product_ext;

    assign product = $signed(act_in) * $signed(weight_q);
    assign product_ext = {{16{product[15]}}, product};

    always_ff @(posedge clk) begin
        if (rst) begin
            weight_q       <= '0;
            act_out        <= '0;
            act_valid_out  <= 1'b0;
            psum_out       <= '0;
            psum_valid_out <= 1'b0;
        end else if (weight_we) begin
            weight_q <= weight_in;
        end else if (pipe_en) begin
            act_out        <= act_in;
            act_valid_out  <= act_valid_in;
            psum_valid_out <= psum_valid_in;
            if (act_valid_in && psum_valid_in)
                psum_out <= psum_in + product_ext;
            else
                psum_out <= psum_in;
        end
    end
endmodule
