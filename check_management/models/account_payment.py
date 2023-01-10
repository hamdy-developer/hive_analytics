# -*- coding: utf-8 -*-

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError


class account_payment_register(models.TransientModel):

    _inherit = "account.payment.register"

    payment_check_lines = fields.Many2many('payment.check.line')
    exist_check = fields.Boolean(string='From Existing Checks', default=False)
    is_check_journal = fields.Boolean(string="is check journal", related="journal_id.is_check")
    is_check_invoice = fields.Boolean(string="is check Invoices", related="journal_id.is_invoice")

    def _init_payments(self, to_process, edit_mode=False):
        res=super(account_payment_register, self)._init_payments(to_process, edit_mode)
        if self.payment_check_lines:
            if len(res) >1:
                raise UserError(_('You have select one partner'))
            self.payment_check_lines.update({'payment_id':res.id})
        return res


    @api.onchange('journal_id')
    def onchange_payment_type_check(self):
        if self.journal_id.is_debit and self.payment_type == 'outbound':
            self.exist_check = True
        else:
            self.exist_check = False



class AccountPayment(models.Model):
    _inherit = "account.payment"

    payment_check_lines = fields.One2many('payment.check.line', 'payment_id')
    is_check_journal = fields.Boolean(string="is check journal", related="journal_id.is_check")
    is_debit_journal = fields.Boolean(string="is Debit journal", related="journal_id.is_debit")
    is_check_invoice = fields.Boolean(string="is check Invoices", related="journal_id.is_invoice")
    total_check_amount = fields.Float(string="Total Check Amount", compute="compute_total_check_amount", store=True,
                                      default=0.0)
    existing_check_lines = fields.Many2many('payment.check.line')
    exist_check = fields.Boolean(string='From Existing Checks', default=False)

    @api.model
    def create(self, vals_list):
        destination_account_id = vals_list.get('destination_account_id', False)
        res = super(AccountPayment, self).create(vals_list)
        res.destination_account_id = destination_account_id
        return res

    # def _seek_for_lines(self):
    #     ''' Helper used to dispatch the journal items between:
    #     - The lines using the temporary liquidity account.
    #     - The lines using the counterpart account.
    #     - The lines being the write-off lines.
    #     :return: (liquidity_lines, counterpart_lines, writeoff_lines)
    #     '''
    #     self.ensure_one()
    #
    #     liquidity_lines = self.env['account.move.line']
    #     counterpart_lines = self.env['account.move.line']
    #     writeoff_lines = self.env['account.move.line']
    #
    #     for line in self.move_id.line_ids:
    #         if line.account_id in self._get_valid_liquidity_accounts():
    #             liquidity_lines += line
    #         elif line.account_id.internal_type in ('receivable', 'payable', 'liquidity','other') or line.partner_id == line.company_id.partner_id:
    #             counterpart_lines += line
    #         else:
    #             writeoff_lines += line
    #
    #     return liquidity_lines, counterpart_lines, writeoff_lines

    def _synchronize_from_moves(self, changed_fields):
        ''' Update the account.payment regarding its related account.move.
        Also, check both models are still consistent.
        :param changed_fields: A set containing all modified fields on account.move.
        '''
        if self._context.get('skip_account_move_synchronization'):
            return

        for pay in self.with_context(skip_account_move_synchronization=True):

            # After the migration to 14.0, the journal entry could be shared between the account.payment and the
            # account.bank.statement.line. In that case, the synchronization will only be made with the statement line.
            if pay.move_id.statement_line_id:
                continue

            move = pay.move_id
            move_vals_to_write = {}
            payment_vals_to_write = {}

            if 'journal_id' in changed_fields:
                if pay.journal_id.type not in ('bank', 'cash'):
                    raise UserError(_("A payment must always belongs to a bank or cash journal."))

            if 'line_ids' in changed_fields:
                all_lines = move.line_ids
                liquidity_lines, counterpart_lines, writeoff_lines = pay._seek_for_lines()

                if len(liquidity_lines) != 1:
                    raise UserError(_(
                        "Journal Entry %s is not valid. In order to proceed, the journal items must "
                        "include one and only one outstanding payments/receipts account.",
                        move.display_name,
                    ))

                # if len(counterpart_lines) != 1:
                #     raise UserError(_(
                #         "Journal Entry %s is not valid. In order to proceed, the journal items must "
                #         "include one and only one receivable/payable account (with an exception of "
                #         "internal transfers).",
                #         move.display_name,
                #     ))

                if writeoff_lines and len(writeoff_lines.account_id) != 1:
                    raise UserError(_(
                        "Journal Entry %s is not valid. In order to proceed, "
                        "all optional journal items must share the same account.",
                        move.display_name,
                    ))

                if any(line.currency_id != all_lines[0].currency_id for line in all_lines):
                    raise UserError(_(
                        "Journal Entry %s is not valid. In order to proceed, the journal items must "
                        "share the same currency.",
                        move.display_name,
                    ))

                if any(line.partner_id != all_lines[0].partner_id for line in all_lines):
                    raise UserError(_(
                        "Journal Entry %s is not valid. In order to proceed, the journal items must "
                        "share the same partner.",
                        move.display_name,
                    ))

                if counterpart_lines.account_id.account_type == 'asset_receivable':
                    partner_type = 'customer'
                else:
                    partner_type = 'supplier'

                liquidity_amount = liquidity_lines.amount_currency

                move_vals_to_write.update({
                    'currency_id': liquidity_lines.currency_id.id,
                    'partner_id': liquidity_lines.partner_id.id,
                })
                payment_vals_to_write.update({
                    'amount': abs(liquidity_amount),
                    'partner_type': partner_type,
                    'currency_id': liquidity_lines.currency_id.id,
                    'destination_account_id': pay.destination_account_id.id,
                    'partner_id': liquidity_lines.partner_id.id,
                })
                if liquidity_amount > 0.0:
                    payment_vals_to_write.update({'payment_type': 'inbound'})
                elif liquidity_amount < 0.0:
                    payment_vals_to_write.update({'payment_type': 'outbound'})

            move.write(move._cleanup_write_orm_values(move, move_vals_to_write))
            pay.write(move._cleanup_write_orm_values(pay, payment_vals_to_write))

    @api.depends('payment_check_lines.check_amount', 'payment_check_lines')
    def compute_total_check_amount(self):
        # print("Compute Total")
        for rec in self:
            total = 0
            if rec.payment_check_lines:
                if rec.is_check_journal:
                    for line in rec.payment_check_lines:
                        if line.state != 'cancel':
                            total += line.check_amount
                    rec.write({'total_check_amount': total})
                    # , 'amount': total})
                    rec.amount = rec.total_check_amount
                else:
                    rec.write({'total_check_amount': rec.total_check_amount})
            else:
                rec.write({'total_check_amount': 0.0})

    # @api.multi
    def action_post(self):
        res = super(AccountPayment, self).action_post()
        for rec in self:
            if rec.payment_check_lines:
                rec.amount = rec.total_check_amount
        return res

    # for smart btn
    # @api.multi
    def button_check_lines(self):
        return {
            'name': _('Check Lines'),
            'view_type': 'form',
            'view_mode': 'tree,form',
            'res_model': 'payment.check.line',
            'view_id': False,
            'type': 'ir.actions.act_window',
            'domain': [('id', 'in', self.payment_check_lines.ids)],
        }

    def compute_existing_check_lines(self):
        if not self.existing_check_lines:
            raise UserError("Warning , Please choose checks")

        for check in self.existing_check_lines:
            self.env['payment.check.line'].create({
                'payment_id': self.id,
                'check_number': check.check_number,
                'check_date': check.check_date,
                'check_amount': check.check_amount,
                'check_bank_id': check.check_bank_id.id,
                'with_drawer_name': check.with_drawer_name,
                'customer_check_id': check.id
            })
            check.state = 'to_vendor'
        return {
            'name': _('Payments'),
            'view_type': 'form',
            'view_mode': 'tree,form',
            'res_model': 'account.payment',
            'view_id': False,
            'type': 'ir.actions.act_window',
            'domain': [('id', '=', self.id)],
        }

    def cancel2(self):
        for rec in self:
            for move in rec.mapped('move_id'):
                if rec.line_ids:
                    move.line_ids.remove_move_reconcile()

    @api.onchange('journal_id')
    def onchange_payment_type_check(self):
        if self.journal_id.is_debit and self.payment_type == 'outbound':
            self.exist_check = True
        else:
            self.exist_check = False

